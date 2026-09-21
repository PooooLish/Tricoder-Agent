#!/usr/bin/env python3
"""贪吃蛇核心逻辑的自动验证脚本（无需交互输入）。"""
import random
from snake_game import SnakeGame, UP, DOWN, LEFT, RIGHT


def test_initial_state():
    g = SnakeGame(width=10, height=10)
    assert len(g.snake) == 1
    assert g.food is not None
    assert g.direction == RIGHT
    assert g.score == 0
    assert not g.game_over
    print("✓ 初始状态正确")


def test_movement():
    g = SnakeGame(width=10, height=10)
    g.set_direction(RIGHT)
    before = g.snake[0]
    g.step()
    assert g.snake[0] == (before[0] + 1, before[1]), "蛇应向右移动"
    print("✓ 移动正确")


def test_no_reverse():
    g = SnakeGame(width=10, height=10)
    # 蛇初始方向向右，尝试向左（反向）应被忽略
    g.set_direction(LEFT)
    assert g.direction == RIGHT, "不应允许 180° 掉头"
    g.set_direction(UP)
    g.step()
    # 向上后尝试向下（反向）应被忽略
    g.set_direction(DOWN)
    assert g.direction == UP, "不应允许 180° 掉头"
    print("✓ 禁止掉头")


def test_wall_collision():
    g = SnakeGame(width=10, height=10)
    # 把蛇放到边界附近并向墙移动
    g.snake = [(0, 5)]
    g.direction = LEFT
    g.step()
    assert g.game_over, "撞墙应结束游戏"
    print("✓ 撞墙检测")


def test_self_collision():
    g = SnakeGame(width=10, height=10)
    # 构造一个会撞到自己的局面
    g.snake = [(5, 5), (4, 5), (4, 6), (5, 6), (6, 6), (6, 5)]
    g.direction = LEFT  # 向 (5,5) 的右侧即自身
    # 移开食物避免意外
    g.food = None
    g._rand_free_cell = lambda: None
    # 让蛇朝自己移动：方向向左，头在 (5,5)，但 (5,5) 是身体一部分
    # 重新构造一个更清晰的自撞
    g.snake = [(5, 5), (6, 5), (6, 6), (5, 6)]
    g.direction = UP  # 向 (5,4)，不与自身冲突
    g.step()
    assert not g.game_over
    # 现在蛇为 head(5,4),(5,5),(6,5),(6,6)
    g.direction = RIGHT  # 向 (6,4) 安全
    g.step()
    assert not g.game_over
    # 转向下撞回自身
    g.direction = DOWN
    # 此刻 head (6,4)，向下到 (6,5) 是自身
    g.step()
    assert g.game_over, "撞自身应结束游戏"
    print("✓ 自撞检测")


def test_food_eaten():
    random.seed(1)
    g = SnakeGame(width=10, height=10)
    # 强制食物在蛇头正前方
    g.snake = [(5, 5)]
    g.direction = RIGHT
    g.food = (6, 5)
    ate = g.step()
    assert ate, "吃到食物应返回 True"
    assert g.score == 1
    assert len(g.snake) == 2, "吃到食物蛇身应增长"
    print("✓ 吃到食物并增长")


def test_food_not_repeat_in_snake():
    g = SnakeGame(width=10, height=10)
    # 跑很多步，确认食物永远不在蛇身上
    steps = 0
    while not g.game_over and steps < 500:
        steps += 1
        assert g.food not in g.snake, "食物不应出现在蛇身上"
        # 随机转向（仅做测试）
        g.set_direction(random.choice([UP, DOWN, LEFT, RIGHT]))
        g.step()
    print(f"✓ 食物位置有效（跑了 {steps} 步）")


if __name__ == "__main__":
    test_initial_state()
    test_movement()
    test_no_reverse()
    test_wall_collision()
    test_self_collision()
    test_food_eaten()
    test_food_not_repeat_in_snake()
    print("\n全部测试通过 ✅")
