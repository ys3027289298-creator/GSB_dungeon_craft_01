import copy
import os
import tempfile
import unittest

from dungeon.core import (
    LOOT_TABLES,
    POISON_DAMAGE,
    Character,
    Game,
)


class TrapTest(unittest.TestCase):
    def test_trap_triggers_only_once(self):
        game = Game()
        self.assertTrue(game.move("next"))
        trap = game.room.traps[0]
        self.assertTrue(trap.triggered)
        self.assertEqual(game.player.hp, 30 - 5)
        # 已触发的陷阱再次触发不造成伤害
        self.assertEqual(trap.trigger(game.player), 0)
        self.assertEqual(game.player.hp, 25)
        # 离开再进入同一房间，陷阱不会重复触发
        game.move("next")
        game.move("prev")
        self.assertEqual(game.player.hp, 25)


class ArmorTest(unittest.TestCase):
    def test_armor_boundary_exact_reduction(self):
        hero = Character("测试", hp=20, attack=5, armor=5)
        # 伤害等于护甲：恰好减到 0，不多减一点
        self.assertEqual(hero.take_damage(5), 0)
        self.assertEqual(hero.hp, 20)
        # 伤害超出护甲 1 点：只受 1 点伤害
        self.assertEqual(hero.take_damage(6), 1)
        self.assertEqual(hero.hp, 19)
        # 伤害低于护甲：不受伤害
        self.assertEqual(hero.take_damage(3), 0)
        self.assertEqual(hero.hp, 19)


class CraftTest(unittest.TestCase):
    def test_failed_craft_rolls_back_materials(self):
        game = Game()
        game.inventory = {"凝胶": 2}
        game.rng.random = lambda: 0.999  # 强制制作失败
        self.assertFalse(game.craft("治疗药水"))
        # 失败后材料必须回滚
        self.assertEqual(game.inventory, {"凝胶": 2})
        # 再次失败仍然幂等
        self.assertFalse(game.craft("治疗药水"))
        self.assertEqual(game.inventory, {"凝胶": 2})
        # 成功时正常消耗并产出
        game.rng.random = lambda: 0.0
        self.assertTrue(game.craft("治疗药水"))
        self.assertEqual(game.inventory, {"治疗药水": 1})

    def test_empty_inventory_boundary(self):
        game = Game()
        self.assertEqual(game.inventory, {})
        # 空背包制作失败且不产生副作用
        self.assertFalse(game.craft("治疗药水"))
        self.assertEqual(game.inventory, {})
        # 未知配方同样安全
        self.assertFalse(game.craft("不存在的配方"))
        self.assertEqual(game.inventory, {})


class RoomRefreshTest(unittest.TestCase):
    def test_leave_room_refreshes_monsters_once(self):
        game = Game()
        game.move("next")  # 陷阱走廊，2 只史莱姆
        for monster in game.room.monsters:
            monster.take_raw_damage(999)
        game.move("next")  # 离开陷阱走廊 -> 刷新一次
        self.assertEqual(len(game.rooms[1].monsters), 2)
        self.assertTrue(all(m.alive for m in game.rooms[1].monsters))
        # 反复进出，怪物数量始终是模板数量，不翻倍
        game.move("prev")
        game.move("next")
        self.assertEqual(len(game.rooms[1].monsters), 2)


class PoisonPauseTest(unittest.TestCase):
    def test_poison_duration_frozen_while_paused(self):
        game = Game()
        game.player.poison_turns = 3
        hp_before = game.player.hp
        game.pause()
        game.pause()  # 暂停幂等
        self.assertTrue(game.paused)
        game.tick()
        self.assertEqual(game.player.poison_turns, 3)
        self.assertEqual(game.player.hp, hp_before)
        game.resume()
        game.resume()  # 恢复幂等
        game.tick()
        self.assertEqual(game.player.poison_turns, 2)
        self.assertEqual(game.player.hp, hp_before - POISON_DAMAGE)


class DeathTest(unittest.TestCase):
    def test_dead_character_cannot_act(self):
        game = Game()
        game.player.take_raw_damage(999)
        self.assertFalse(game.player.alive)
        # 死亡后不能移动、战斗、制作
        self.assertFalse(game.move("next"))
        self.assertIsNone(game.fight_round())
        self.assertFalse(game.craft("骨剑"))
        self.assertEqual(game.current, 0)
        self.assertEqual(game.round, 0)
        # 死亡状态幂等：重复伤害不改变状态
        self.assertEqual(game.player.take_raw_damage(999), 0)
        self.assertEqual(game.player.hp, 0)
        self.assertFalse(game.player.alive)


class LootTableTest(unittest.TestCase):
    def test_drops_do_not_mutate_global_template(self):
        snapshot = copy.deepcopy(LOOT_TABLES)
        game = Game()
        game.rng.random = lambda: 0.0  # 强制全部掉落
        slime = game.rooms[1].template_monsters[0]
        drops = game.roll_drops(slime)
        self.assertEqual(drops, ["凝胶"])
        self.assertEqual(game.inventory, {"凝胶": 1})
        # 全局掉落表不被修改，可重复掉落
        self.assertEqual(LOOT_TABLES, snapshot)
        game.roll_drops(slime)
        self.assertEqual(LOOT_TABLES, snapshot)
        self.assertEqual(game.inventory, {"凝胶": 2})


class UndoTest(unittest.TestCase):
    def test_undo_move_restores_trap_state(self):
        game = Game()
        game.move("next")
        self.assertTrue(game.rooms[1].traps[0].triggered)
        self.assertEqual(game.player.hp, 25)
        self.assertTrue(game.undo_move())
        # 撤销后位置、血量与陷阱状态全部恢复
        self.assertEqual(game.current, 0)
        self.assertEqual(game.player.hp, 30)
        self.assertFalse(game.rooms[1].traps[0].triggered)
        # 再次进入，陷阱可以重新触发一次
        game.move("next")
        self.assertTrue(game.rooms[1].traps[0].triggered)
        self.assertEqual(game.player.hp, 25)
        # 没有历史时撤销幂等失败
        fresh = Game()
        self.assertFalse(fresh.undo_move())


class SaveLoadTest(unittest.TestCase):
    def test_load_does_not_double_settle_round(self):
        game = Game()
        game.move("next")
        game.fight_round()
        monster_hp = game.rooms[1].monsters[0].hp
        player_hp = game.player.hp
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            game.save(path)
            loaded = Game.load(path)
        # 读档后状态与存档一致，没有重复结算
        self.assertEqual(loaded.round, game.round)
        self.assertEqual(loaded.rooms[1].monsters[0].hp, monster_hp)
        self.assertEqual(loaded.player.hp, player_hp)
        # 之后每调用一次只结算一轮
        dealt = max(0, loaded.player.attack - loaded.rooms[1].monsters[0].armor)
        loaded.fight_round()
        self.assertEqual(loaded.round, game.round + 1)
        self.assertEqual(loaded.rooms[1].monsters[0].hp,
                         max(0, monster_hp - dealt))


if __name__ == "__main__":
    unittest.main()
