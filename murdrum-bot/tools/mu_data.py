"""Извлечение данных из клиента Murdrum MU (Season 2) для бота.

    python mu_data.py "C:/Games/Murdrum"          # распаковать карты в ../maps
    python mu_data.py --path lorencia 130 130 180 120   # проложить путь

Карта проходимости (EncTerrainN.att): 256x256 клеток, байт-флаги:
    0x01 безопасная зона, 0x04 стена/нельзя ходить, 0x08 пустота (нет земли).
Шифрование: MapFileDecrypt (XOR-таблица 16 байт с переносом ключа) + BuxConvert (FC CF AB).
"""
import heapq
import json
import os
import sys

SAFE, NOMOVE, NOGROUND = 0x01, 0x04, 0x08

# Номер папки WorldN -> имя карты (N = номер карты + 1)
MAPS = {
    1: "lorencia", 2: "dungeon", 3: "devias", 4: "noria", 5: "losttower",
    7: "arena", 8: "atlans", 9: "tarkan", 11: "icarus",
}

MAP_KEY = [0xD1, 0x73, 0x52, 0xF6, 0xD2, 0x9A, 0xCB, 0x27,
           0x3E, 0xAF, 0x59, 0x31, 0x37, 0xB3, 0xE7, 0xA2]
BUX_KEY = [0xFC, 0xCF, 0xAB]


def map_decrypt(data: bytes) -> bytes:
    out = bytearray(len(data))
    w = 0x5E
    for i, c in enumerate(data):
        out[i] = ((c ^ MAP_KEY[i % 16]) - w) & 0xFF
        w = (c + 0x3D) & 0xFF
    return bytes(out)


def bux(data: bytes) -> bytes:
    return bytes(c ^ BUX_KEY[i % 3] for i, c in enumerate(data))


def read_att(path: str) -> bytes:
    """Вернуть 65536 байт флагов (индекс = y*256 + x)."""
    d = bux(map_decrypt(open(path, "rb").read()))[4:]
    if len(d) == 131072:  # WORD-формат: берём младший байт
        d = d[0::2]
    if len(d) != 65536:
        raise ValueError(f"{path}: неожиданный размер {len(d)}")
    return d


def read_movereq(path: str):
    """Список команд /move: имя, мин. уровень, цена в зен, номер гейта."""
    raw = open(path, "rb").read()[4:]
    res = []
    for i in range(len(raw) // 80):
        r = bux(raw[i * 80:(i + 1) * 80])
        name = r[4:36].split(b"\0")[0].decode("cp1251", "replace")
        if not name:
            continue
        lvl, zen, gate = (int.from_bytes(r[o:o + 4], "little", signed=True) for o in (68, 72, 76))
        res.append({"name": name, "level": lvl, "zen": zen, "gate": gate})
    return res


def extract(client_dir: str, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    data = os.path.join(client_dir, "Data")
    for n, name in MAPS.items():
        p = os.path.join(data, f"World{n}", f"EncTerrain{n}.att")
        if not os.path.exists(p):
            print("нет", p)
            continue
        with open(os.path.join(out_dir, f"{name}.att"), "wb") as f:
            f.write(read_att(p))
        print("ok", name)
    mv = read_movereq(os.path.join(data, "Local", "Movereq.bmd"))
    with open(os.path.join(out_dir, "movereq.json"), "w", encoding="utf-8") as f:
        json.dump(mv, f, ensure_ascii=False, indent=1)
    print("movereq:", len(mv), "команд")


def load_map(name: str, maps_dir: str) -> bytes:
    return open(os.path.join(maps_dir, f"{name}.att"), "rb").read()


def walkable(grid: bytes, x: int, y: int) -> bool:
    return 0 <= x < 256 and 0 <= y < 256 and not (grid[y * 256 + x] & (NOMOVE | NOGROUND))


def _neighbors(grid: bytes, cx: int, cy: int):
    """Соседи по 8 направлениям без срезания углов стен: (x, y, цена шага)."""
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if not dx and not dy:
                continue
            nx, ny = cx + dx, cy + dy
            if not walkable(grid, nx, ny):
                continue
            if dx and dy and not (walkable(grid, cx + dx, cy) and walkable(grid, cx, cy + dy)):
                continue
            yield nx, ny, (14 if dx and dy else 10)


def find_path(grid: bytes, start, goal, max_nodes=200000):
    """A* по 8 направлениям. Возвращает список клеток [(x, y), ...] или None."""
    if not walkable(grid, *start) or not walkable(grid, *goal):
        return None
    openq = [(0, 0, start)]
    came = {start: None}
    cost = {start: 0}
    n = 0
    while openq and n < max_nodes:
        _, g, cur = heapq.heappop(openq)
        if g > cost[cur]:
            continue  # устаревшая запись очереди
        n += 1
        if cur == goal:
            path = []
            while cur is not None:
                path.append(cur)
                cur = came[cur]
            return path[::-1]
        for nx, ny, step in _neighbors(grid, *cur):
            nc = g + step
            if nc < cost.get((nx, ny), 1 << 30):
                cost[(nx, ny)] = nc
                came[(nx, ny)] = cur
                h = 10 * max(abs(goal[0] - nx), abs(goal[1] - ny))
                heapq.heappush(openq, (nc + h, nc, (nx, ny)))
    return None


def waypoints(path, step=6):
    """Сократить путь до точек каждые `step` клеток (бот кликает по ним)."""
    if not path:
        return []
    pts = path[step::step]
    if pts[-1:] != path[-1:]:
        pts.append(path[-1])
    return pts


def is_safe(grid: bytes, x: int, y: int) -> bool:
    return bool(grid[y * 256 + x] & SAFE)


def drop_spot(grid: bytes, pos, min_dist=6, max_dist=10):
    """Ближайшая достижимая клетка в min..max клетках (по прямой) от pos, вне безопасной зоны.

    Туда бот уходит выбрасывать мусор, чтобы Space на споте его не подбирал.
    Один обход в ширину от pos, а не A* на каждого кандидата. None, если такой клетки нет.
    """
    if not walkable(grid, *pos):
        return None
    px, py = pos
    seen = {pos}
    frontier = [pos]
    # путь в обход стен может быть длиннее прямой — ищем с запасом
    for _ in range(max_dist * 3):
        nxt = []
        for cx, cy in frontier:
            for nx, ny, _step in _neighbors(grid, cx, cy):
                if (nx, ny) in seen:
                    continue
                seen.add((nx, ny))
                d = max(abs(nx - px), abs(ny - py))
                if d > max_dist:
                    continue
                if d >= min_dist and not is_safe(grid, nx, ny):
                    return (nx, ny)
                nxt.append((nx, ny))
        if not nxt:
            break
        frontier = nxt
    return None


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    maps_dir = os.path.join(here, "..", "maps")
    if len(sys.argv) >= 7 and sys.argv[1] == "--path":
        g = load_map(sys.argv[2], maps_dir)
        s = (int(sys.argv[3]), int(sys.argv[4]))
        t = (int(sys.argv[5]), int(sys.argv[6]))
        p = find_path(g, s, t)
        print("нет пути" if p is None else f"{len(p)} клеток, точки: {waypoints(p)}")
    elif len(sys.argv) == 2:
        extract(sys.argv[1], maps_dir)
    else:
        print(__doc__)
