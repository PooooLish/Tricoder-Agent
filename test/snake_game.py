#!/usr/bin/env python3
"""贪吃蛇小游戏 (控制台版)
使用方向键 / WASD 控制蛇的移动。
"""
import sys
import time
import random
import select
import os


WIDTH = 20
HEIGHT = 20

# 方向向量
UP = (0, -1)
DOWN = (0, 1)
LEFT = (-1, 0)
RIGHT = (1, 0)

KEYS = {
    "w": UP, "W": UP, "\x1b[A": UP, "k": UP,
    "s": DOWN, "S": DOWN, "\x1b[B": DOWN, "j": DOWN,
    "a": LEFT, "A": LEFT, "\x1b[D": LEFT, "h": LEFT,
    "d": RIGHT, "D": RIGHT, "\x1b[C": RIGHT, "l": RIGHT,
}


class SnakeGame:
    def __init__(self, width=WIDTH, height=HEIGHT):
        self.width = width
        self.height = height
        self.reset()

    def reset(self):
        """初始化游戏状态。"""
        self.snake = [(self.width // 2, self.height // 2)]
        self.direction = RIGHT
        self.score = 0
        self.game_over = False
        self.won = False
        self._spawn_food()

    def _rand_free_cell(self):
        """在蛇身之外随机生成坐标。"""
        occupied = set(self.snake)
        free = [
            (x, y)
            for x in range(self.width)
            for y in range(self.height)
            if (x, y) not in occupied
        ]
        if not free:
            return None
        return random.choice(free)

    def _spawn_food(self):
        pos = self._rand_free_cell()
        if pos is None:
            self.food = None
            self.won = True
        else:
            self.food = pos

    def set_direction(self, new_dir):
        """改变方向，不允许 180° 掉头。"""
        if (new_dir[0] == -self.direction[0]
                and new_dir[1] == -self.direction[1]):
            return
        self.direction = new_dir

    def step(self):
        """蛇前进一步，返回是否还有食物被吃掉。"""
        if self.game_over:
            return False
        head = self.snake[0]
        new_head = (head[0] + self.direction[0],
                    head[1] + self.direction[1])

        # 撞墙检测（经典模式：撞墙结束）
        if (new_head[0] < 0 or new_head[0] >= self.width
                or new_head[1] < 0 or new_head[1] >= self.height):
            self.game_over = True
            return False

        # 撞自身检测
        if new_head in self.snake:
            self.game_over = True
            return False

        self.snake.insert(0, new_head)

        if new_head == self.food:
            self.score += 1
            self._spawn_food()
            return True
        else:
            self.snake.pop()
            return False


class TerminalRenderer:
    """在终端渲染贪吃蛇。"""

    def __init__(self, width, height):
        self.width = width
        self.height = height

    def render(self, game):
        """渲染一帧画面。"""
        out = []
        # 顶部边框
        out.append("+" + "-" * self.width * 2 + "+")
        # 将蛇身转为集合，使每一格的成员判断为 O(1)，避免对列表逐项扫描
        body = set(game.snake)
        head = game.snake[0]
        for y in range(self.height):
            row = ["|"]
            for x in range(self.width):
                ch = "  "
                if (x, y) == game.food:
                    ch = " *"
                elif (x, y) == head:
                    ch = " 0"
                elif (x, y) in body:
                    ch = " o"
                row.append(ch)
            row.append("|")
            out.append("".join(row))
        out.append("+" + "-" * self.width * 2 + "+")
        out.append("")
        out.append(f"Score: {game.score}")
        self._clear_screen()
        print("\n".join(out))
        print("方向键/WASD 移动 | q 退出 | r 重新开始")

    @staticmethod
    def _clear_screen():
        if os.name == "nt":
            os.system("cls")
        else:
            os.system("clear")


def is_key_ready():
    """检查是否有键盘输入（跨平台）。"""
    if os.name == "nt":
        import msvcrt
        return msvcrt.kbhit()
    return select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], [])


def read_key():
    """读取按键（支持方向键的转义序列）。"""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            ch2 = msvcrt.getch()
            return {b"H": "\x1b[A",
                    b"P": "\x1b[B",
                    b"K": "\x1b[D",
                    b"M": "\x1b[C"}.get(ch2, "")
        return ch.decode("utf-8", "ignore")
    else:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            first = sys.stdin.read(1)
            if first == "\x1b":
                rest = sys.stdin.read(2)
                return first + rest
            return first
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def enable_raw_mode():
    """进入原始终端模式以捕获按键。非 Windows 下生效。"""
    if os.name != "nt":
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        return old


def restore_terminal(state):
    if os.name != "nt" and state is not None:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, state)


def main():
    game = SnakeGame()
    renderer = TerminalRenderer(game.width, game.height)

    if os.name == "nt":
        import msvcrt
    saved_state = enable_raw_mode()
    renderer.render(game)

    speed = 0.15
    try:
        while True:
            # 处理输入
            while is_key_ready():
                key = read_key()
                if key.lower() == "q":
                    renderer._clear_screen()
                    print("再见！")
                    return
                if key.lower() == "r":
                    game.reset()
                    renderer.render(game)
                    continue
                if key in KEYS:
                    game.set_direction(KEYS[key])

            if game.game_over:
                renderer.render(game)
                print("游戏结束！按 r 重新开始，按 q 退出")
                time.sleep(0.3)
                continue

            game.step()
            renderer.render(game)

            if game.won:
                print("🎉 你赢了！填满了整个棋盘！")
                time.sleep(2)
                game.reset()
                renderer.render(game)

            time.sleep(speed)
    finally:
        restore_terminal(saved_state)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
