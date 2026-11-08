"""Тесты для mu_data.py. Запуск: python test_mu_data.py (или pytest)."""
import os
import random

import mu_data as m

MAPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "maps")


def grid(name):
    return m.load_map(name, MAPS)


def test_maps_decoded():
    for name in ("lorencia", "dungeon", "devias", "noria", "losttower"):
        g = grid(name)
        assert len(g) == 65536, name
        walk = sum(1 for b in g if not b & (m.NOMOVE | m.NOGROUND))
        assert 5000 < walk < 60000, (name, walk)  # мусорная расшифровка дала бы шум
    # центр Лоренсии — безопасная зона города
    assert m.is_safe(grid("lorencia"), 130, 130)


def test_bux_roundtrip():
    data = bytes(range(256))
    assert m.bux(m.bux(data)) == data


def test_path_lorencia():
    g = grid("lorencia")
    p = m.find_path(g, (130, 130), (180, 120))
    assert p is not None
    assert p[0] == (130, 130) and p[-1] == (180, 120)
    assert 50 <= len(p) <= 60
    for (x1, y1), (x2, y2) in zip(p, p[1:]):
        assert max(abs(x2 - x1), abs(y2 - y1)) == 1
        assert m.walkable(g, x2, y2)


def test_path_to_wall_is_none():
    g = grid("lorencia")
    wall = next((x, y) for y in range(256) for x in range(256) if not m.walkable(g, x, y))
    assert m.find_path(g, (130, 130), wall) is None
    assert m.find_path(g, wall, (130, 130)) is None


def test_waypoints_end_at_goal():
    g = grid("lorencia")
    p = m.find_path(g, (130, 130), (180, 120))
    wp = m.waypoints(p)
    assert wp[-1] == (180, 120)


def test_drop_spot_outside_safe_zone_and_far_enough():
    rnd = random.Random(1)
    for name in ("lorencia", "devias", "dungeon", "losttower"):
        g = grid(name)
        cells = [(x, y) for y in range(256) for x in range(256) if m.walkable(g, x, y)]
        for p in rnd.sample(cells, 150):
            s = m.drop_spot(g, p)
            if s is None:
                continue
            assert not m.is_safe(g, *s), (name, p, s)
            assert 6 <= max(abs(s[0] - p[0]), abs(s[1] - p[1])) <= 10, (name, p, s)
            assert m.find_path(g, p, s) is not None, (name, p, s)


def test_drop_spot_town_center_has_none():
    # в радиусе 10 клеток от центра города всё — безопасная зона
    assert m.drop_spot(grid("lorencia"), (130, 130)) is None


def test_movereq_json():
    import json
    with open(os.path.join(MAPS, "movereq.json"), encoding="utf-8") as f:
        mv = json.load(f)
    names = {x["name"] for x in mv}
    assert {"Lorencia", "Dungeon2", "LostTower", "Atlans"} <= names
    assert all(x["zen"] > 0 and x["level"] > 0 for x in mv)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"все {len(tests)} тестов прошли")
