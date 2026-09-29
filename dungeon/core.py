"""地牢制作游戏核心逻辑。

包含战斗、制作、移动、暂停、死亡、掉落与撤销。
所有公开操作均为幂等：重复调用不会产生额外副作用。
"""

from __future__ import annotations

import copy
import json
import random

FIXED_SEED = 20260929
POISON_DAMAGE = 2

LOOT_TABLES = {
    "史莱姆": [{"item": "凝胶", "chance": 0.9, "count": 1}],
    "骷髅": [{"item": "骨头", "chance": 0.9, "count": 2}],
}

RECIPES = {
    "治疗药水": {"materials": {"凝胶": 2}, "result": "治疗药水", "success_rate": 0.5},
    "骨剑": {"materials": {"骨头": 3}, "result": "骨剑", "success_rate": 1.0},
}


class Character:
    def __init__(self, name, hp, attack, armor=0):
        self.name = name
        self.max_hp = hp
        self.hp = hp
        self.attack = attack
        self.armor = armor
        self.poison_turns = 0
        self.alive = True

    def take_raw_damage(self, amount):
        # 死亡角色不再承受伤害：重复调用返回 0 且状态不变（幂等）
        if not self.alive:
            return 0
        amount = max(0, amount)
        self.hp = max(0, self.hp - amount)
        if self.hp == 0:
            self.alive = False
        return amount

    def take_damage(self, amount):
        # 护甲减伤：伤害等于护甲时恰好减到 0，临界值不多减一点
        reduced = max(0, amount - self.armor)
        self.take_raw_damage(reduced)
        return reduced

    def to_dict(self):
        return {
            "name": self.name,
            "max_hp": self.max_hp,
            "hp": self.hp,
            "attack": self.attack,
            "armor": self.armor,
            "poison_turns": self.poison_turns,
            "alive": self.alive,
        }

    @classmethod
    def from_dict(cls, data):
        obj = cls(data["name"], data["max_hp"], data["attack"], data["armor"])
        obj.hp = data["hp"]
        obj.poison_turns = data["poison_turns"]
        obj.alive = data["alive"]
        return obj


class Monster(Character):
    def __init__(self, name, hp, attack, armor=0, loot_key=None):
        super().__init__(name, hp, attack, armor)
        self.loot_key = loot_key

    def to_dict(self):
        data = super().to_dict()
        data["loot_key"] = self.loot_key
        return data

    @classmethod
    def from_dict(cls, data):
        obj = cls(data["name"], data["max_hp"], data["attack"], data["armor"],
                  data.get("loot_key"))
        obj.hp = data["hp"]
        obj.poison_turns = data["poison_turns"]
        obj.alive = data["alive"]
        return obj


class Trap:
    def __init__(self, name, damage):
        self.name = name
        self.damage = damage
        self.triggered = False

    def trigger(self, target):
        # 陷阱只触发一次：已触发时重复调用返回 0，不造成伤害（幂等）
        if self.triggered:
            return 0
        self.triggered = True
        return target.take_raw_damage(self.damage)

    def to_dict(self):
        return {"name": self.name, "damage": self.damage, "triggered": self.triggered}

    @classmethod
    def from_dict(cls, data):
        obj = cls(data["name"], data["damage"])
        obj.triggered = data["triggered"]
        return obj


class Room:
    def __init__(self, name, monsters=None, traps=None):
        self.name = name
        self.monsters = list(monsters or [])
        self.traps = list(traps or [])
        # 房间模板：刷新时整体重置回模板，绝不原地追加
        self.template_monsters = copy.deepcopy(self.monsters)

    def refresh(self):
        # 刷新幂等：每次都重置为模板快照，重复调用怪物数量不翻倍
        self.monsters = copy.deepcopy(self.template_monsters)

    def to_dict(self):
        return {
            "name": self.name,
            "monsters": [m.to_dict() for m in self.monsters],
            "traps": [t.to_dict() for t in self.traps],
            "template_monsters": [m.to_dict() for m in self.template_monsters],
        }

    @classmethod
    def from_dict(cls, data):
        room = cls(data["name"])
        room.monsters = [Monster.from_dict(m) for m in data["monsters"]]
        room.traps = [Trap.from_dict(t) for t in data["traps"]]
        room.template_monsters = [Monster.from_dict(m) for m in data["template_monsters"]]
        return room


def _build_dungeon():
    return [
        Room("入口"),
        Room("陷阱走廊",
             monsters=[Monster("史莱姆", 12, 4, 1, "史莱姆"),
                       Monster("史莱姆", 12, 4, 1, "史莱姆")],
             traps=[Trap("地刺", 5)]),
        Room("骷髅厅",
             monsters=[Monster("骷髅", 20, 6, 2, "骷髅")]),
    ]


def _rng_state_to_json(state):
    return [state[0], list(state[1]), state[2]]


def _rng_state_from_json(data):
    return (data[0], tuple(data[1]), data[2])


class Game:
    def __init__(self, seed=FIXED_SEED):
        self.seed = seed
        self.rng = random.Random(seed)
        self.player = Character("勇者", hp=30, attack=8, armor=2)
        self.inventory = {}
        self.rooms = _build_dungeon()
        self.current = 0
        self.round = 0
        self.paused = False
        self._history = []

    @property
    def room(self):
        return self.rooms[self.current]

    # ---- 移动与撤销 ----
    def move(self, direction):
        # 死亡或暂停时不能移动
        if not self.player.alive or self.paused:
            return False
        if direction == "next":
            target = self.current + 1
        elif direction == "prev":
            target = self.current - 1
        else:
            return False
        if not 0 <= target < len(self.rooms):
            return False
        self._history.append(self.to_dict())
        departed = self.room
        self.current = target
        # 离开房间时只刷新一次；refresh 自身幂等，反复进出不会追加怪物
        departed.refresh()
        for trap in self.room.traps:
            trap.trigger(self.player)
        self.tick()
        return True

    def undo_move(self):
        # 撤销 = 完整恢复移动前快照，陷阱状态、血量、怪物、随机数一并还原
        if not self._history:
            return False
        self._restore(self._history.pop())
        return True

    # ---- 战斗 ----
    def fight_round(self):
        # 暂停或死亡时不结算；无可战斗目标时也不推进轮次（幂等）
        if self.paused or not self.player.alive:
            return None
        monster = next((m for m in self.room.monsters if m.alive), None)
        if monster is None:
            return None
        self.round += 1
        dealt = monster.take_damage(self.player.attack)
        result = {"round": self.round, "dealt": dealt, "taken": 0, "drops": []}
        if not monster.alive:
            result["drops"] = self.roll_drops(monster)
        else:
            result["taken"] = self.player.take_damage(monster.attack)
        return result

    # ---- 制作 ----
    def craft(self, recipe_name):
        if not self.player.alive or self.paused:
            return False
        recipe = RECIPES.get(recipe_name)
        if recipe is None:
            return False
        materials = recipe["materials"]
        # 先校验材料是否齐全（空背包也安全），不足则直接失败、不产生任何副作用
        if any(self.inventory.get(item, 0) < need
               for item, need in materials.items()):
            return False
        for item, need in materials.items():
            self.inventory[item] -= need
            if self.inventory[item] == 0:
                del self.inventory[item]
        if self.rng.random() < recipe["success_rate"]:
            result_item = recipe["result"]
            self.inventory[result_item] = self.inventory.get(result_item, 0) + 1
            return True
        # 制作失败：回滚已消耗的全部材料，保证重复失败幂等
        for item, need in materials.items():
            self.inventory[item] = self.inventory.get(item, 0) + need
        return False

    # ---- 掉落 ----
    def roll_drops(self, monster):
        key = getattr(monster, "loot_key", None)
        if not key or key not in LOOT_TABLES:
            return []
        # 使用全局掉落表的深拷贝，任何修改都不会污染 LOOT_TABLES
        entries = copy.deepcopy(LOOT_TABLES[key])
        drops = []
        for entry in entries:
            if self.rng.random() < entry["chance"]:
                item = entry["item"]
                self.inventory[item] = self.inventory.get(item, 0) + entry["count"]
                drops.append(item)
        return drops

    # ---- 暂停与状态推进 ----
    def pause(self):
        self.paused = True
        return True

    def resume(self):
        self.paused = False
        return True

    def tick(self):
        # 暂停时时间冻结：中毒既不造成伤害，持续时间也不减少
        if self.paused:
            return False
        if self.player.alive and self.player.poison_turns > 0:
            self.player.take_raw_damage(POISON_DAMAGE)
            self.player.poison_turns -= 1
        return True

    # ---- 序列化（JSON 存档 / 撤销快照） ----
    def to_dict(self):
        return {
            "seed": self.seed,
            "player": self.player.to_dict(),
            "inventory": dict(self.inventory),
            "rooms": [r.to_dict() for r in self.rooms],
            "current": self.current,
            "round": self.round,
            "paused": self.paused,
            "rng_state": _rng_state_to_json(self.rng.getstate()),
        }

    def _restore(self, data):
        self.seed = data["seed"]
        self.player = Character.from_dict(data["player"])
        self.inventory = dict(data["inventory"])
        self.rooms = [Room.from_dict(r) for r in data["rooms"]]
        self.current = data["current"]
        self.round = data["round"]
        self.paused = data["paused"]
        self.rng = random.Random(self.seed)
        self.rng.setstate(_rng_state_from_json(data["rng_state"]))

    @classmethod
    def from_dict(cls, data):
        game = cls.__new__(cls)
        game._history = []
        game._restore(data)
        return game

    def save(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path):
        # 读档只恢复状态，不触发任何战斗结算，避免轮次被重复结算
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def load_into(self, path):
        with open(path, encoding="utf-8") as fh:
            self._restore(json.load(fh))
        self._history = []
        return True

    # ---- CMD 菜单 ----
    def menu(self):
        return (
            "=== 地牢制作 ===\n"
            "1) 前进  2) 后退  3) 战斗一轮  4) 制作 <配方名>\n"
            "5) 暂停/继续  6) 撤销移动  7) 存档 [路径]  8) 读档 [路径]  0) 退出"
        )

    def handle_command(self, cmd):
        parts = cmd.strip().split(maxsplit=1)
        if not parts:
            return "无效命令"
        op = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        if op == "1":
            return "前进到 " + self.room.name if self.move("next") else "无法前进"
        if op == "2":
            return "后退到 " + self.room.name if self.move("prev") else "无法后退"
        if op == "3":
            result = self.fight_round()
            if result is None:
                return "无法战斗"
            return "第 {} 轮：造成 {} 点伤害，受到 {} 点伤害".format(
                result["round"], result["dealt"], result["taken"])
        if op == "4":
            return "制作成功" if self.craft(arg) else "制作失败"
        if op == "5":
            if self.paused:
                self.resume()
                return "继续游戏"
            self.pause()
            return "游戏暂停"
        if op == "6":
            return "已撤销移动" if self.undo_move() else "没有可撤销的移动"
        if op == "7":
            self.save(arg or "save.json")
            return "已存档"
        if op == "8":
            self.load_into(arg or "save.json")
            return "已读档"
        if op == "0":
            return "退出"
        return "未知命令"


def main():
    game = Game()
    print(game.menu())
    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd in ("0", "quit", "exit"):
            break
        print(game.handle_command(cmd))


if __name__ == "__main__":
    main()
