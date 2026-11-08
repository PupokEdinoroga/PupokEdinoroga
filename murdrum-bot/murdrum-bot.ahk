; Murdrum farm bot (AutoHotkey v2)
; Внешний бот: смотрит на пиксели экрана и жмёт клавиши/мышь.
; Не читает память клиента, не трогает файлы игры и сетевой трафик.
;
; F8   — старт / стоп
; F10  — калибровка (подсказка с координатами и цветом под курсором)
; Ctrl+Alt+C — скопировать "x,y цвет" точки под курсором в буфер
; F11  — записать цвета ПУСТОГО инвентаря (инвентарь открыт и пуст)
; Ctrl+F11 — записать цвета ПУСТОГО сундука (сундук открыт и пуст)
; Ctrl+F10 — проверить сетку инвентаря (курсор пробежит по ячейкам)
; F12  — выход

#Requires AutoHotkey v2.0
#SingleInstance Force
Persistent

global INI := A_ScriptDir "\settings.ini"
global LOGFILE := A_ScriptDir "\bot.log"
global Running := false
global Cfg := Map()
global SpotIdx := 1
global DeathTimes := []
global LastPickup := 0
global LastClean := 0
global LastSkill := 0
global LastRClick := 0
global AttackHeld := false
global CalibOn := false

CoordMode "Mouse", "Client"
CoordMode "Pixel", "Client"
CoordMode "ToolTip", "Screen"
SendMode "Event"
SetKeyDelay 40, 40
SetMouseDelay 30
SetDefaultMouseSpeed 2
OnExit(ReleaseAll)

LoadCfg()
Status("готов. F8 — старт/стоп, F10 — калибровка, F12 — выход")

; ---------------------------------------------------------------- hotkeys
F8:: {
    global Running
    Running := !Running
    if Running {
        Log("=== СТАРТ ===")
        SetTimer(MainLoop, -10)
    } else {
        Log("=== СТОП ===")
        ReleaseAll()
        Status("остановлен (F8 — старт)")
    }
}
F10:: ToggleCalib()
^F10:: CheckGrid("Inventory")
F11:: RecordGrid("Inventory")
^F11:: RecordGrid("Vault")
^!c:: CopyPoint()
F12:: ExitApp()

; ---------------------------------------------------------------- config
LoadCfg() {
    global Cfg
    defaults := Map(
        "Game.Window", "ahk_exe main.exe",
        "Game.Exe", "main.exe",
        "Game.Launcher", "",
        "Game.LauncherSteps", "",
        "Game.LauncherWait", 90000,
        "Game.Login", "",
        "Game.Password", "",
        "Game.LoginSteps", "",
        "Game.ReconnectPause", 30000,
        "Farm.GoToSpotOnStart", 0,
        "Farm.AttackMode", "hold",
        "Farm.AttackX", 400,
        "Farm.AttackY", 260,
        "Farm.RClickEvery", 700,
        "Farm.SkillKey", "",
        "Farm.SkillEvery", 1000,
        "Farm.PickupKey", "{Space}",
        "Farm.PickupEvery", 3000,
        "Farm.PickupPresses", 3,
        "Farm.CleanEvery", 60000,
        "Inventory.Key", "v",
        "Inventory.X", 0,
        "Inventory.Y", 0,
        "Inventory.CellW", 20,
        "Inventory.CellH", 20,
        "Inventory.Cols", 8,
        "Inventory.Rows", 8,
        "Inventory.Tolerance", 14,
        "Inventory.ImageTol", 40,
        "Inventory.Mode", "keep",
        "Inventory.MinFree", 4,
        "Inventory.DropX", 200,
        "Inventory.DropY", 260,
        "Inventory.AfterDropSteps", "",
        "Vault.Steps", "",
        "Vault.CloseSteps", "key:{Esc}",
        "Vault.X", 0,
        "Vault.Y", 0,
        "Vault.CellW", 20,
        "Vault.CellH", 20,
        "Vault.Cols", 8,
        "Vault.Rows", 15,
        "Vault.Tolerance", 14,
        "Health.X", 0,
        "Health.Y", 0,
        "Health.Color", "",
        "Health.Tolerance", 40,
        "Health.RespawnWait", 8000,
        "Spots.DeathsToSwitch", 2,
        "Spots.DeathWindowMin", 10,
        "Disconnect.Image", "",
        "Disconnect.CheckEvery", 5000
    )
    Cfg := Map()
    for k, def in defaults {
        p := StrSplit(k, ".")
        Cfg[k] := Trim(IniRead(INI, p[1], p[2], def))
    }
}

N(key) {
    v := Cfg[key]
    return (v = "") ? 0 : Integer(v)
}

; ---------------------------------------------------------------- main loop
MainLoop() {
    global LastPickup, LastClean, Running
    LoadCfg()
    if !WinExist(Cfg["Game.Window"]) {
        Log("окно игры не найдено (" Cfg["Game.Window"] ") — пробую войти")
        if !Reconnect() {
            Running := false
            Status("не удалось войти в игру, смотри bot.log")
            return
        }
    }
    EnsureActive()
    if N("Farm.GoToSpotOnStart")
        GoToSpot()
    LastPickup := A_TickCount
    LastClean := A_TickCount
    lastDc := A_TickCount
    deadHits := 0
    Status("фарм, спот " SpotIdx)

    while Running {
        now := A_TickCount

        ; --- дисконнект
        if (now - lastDc >= N("Disconnect.CheckEvery")) {
            lastDc := now
            if IsDisconnected() {
                if !Reconnect() {
                    Log("перезаход не удался, пауза")
                    Wait(N("Game.ReconnectPause"))
                }
                lastDc := 0
                continue
            }
        }

        EnsureActive()

        ; --- смерть
        if IsDead() {
            deadHits++
            if (deadHits >= 3) {
                deadHits := 0
                OnDeath()
                continue
            }
        } else {
            deadHits := 0
        }

        ; --- атака
        AttackTick()

        ; --- подбор
        if (now - LastPickup >= N("Farm.PickupEvery")) {
            AttackOff()
            Loop N("Farm.PickupPresses") {
                Send Cfg["Farm.PickupKey"]
                Sleep 150
            }
            LastPickup := A_TickCount
        }

        ; --- чистка инвентаря
        if (now - LastClean >= N("Farm.CleanEvery")) {
            AttackOff()
            res := CleanInventory()
            LastClean := A_TickCount
            if (res.free >= 0) {
                Log("инвентарь: выброшено " res.dropped ", свободно ячеек " res.free)
                if (res.dropped > 0)
                    RunSteps(Cfg["Inventory.AfterDropSteps"])
                if (res.free < N("Inventory.MinFree"))
                    StashTrip()
            }
        }

        Sleep 100
    }
    ReleaseAll()
}

; ---------------------------------------------------------------- attack
AttackTick() {
    global AttackHeld, LastSkill, LastRClick
    mode := StrLower(Cfg["Farm.AttackMode"])
    x := N("Farm.AttackX"), y := N("Farm.AttackY")
    if (mode = "hold") {
        if !AttackHeld {
            MouseMove x, y
            Click "Down Right"
            AttackHeld := true
        }
    } else if (mode = "click") {
        if (A_TickCount - LastRClick >= N("Farm.RClickEvery")) {
            Click x " " y " Right"
            LastRClick := A_TickCount
        }
    }
    if (Cfg["Farm.SkillKey"] != "" && A_TickCount - LastSkill >= N("Farm.SkillEvery")) {
        Send Cfg["Farm.SkillKey"]
        LastSkill := A_TickCount
    }
}

AttackOff() {
    global AttackHeld
    if AttackHeld {
        Click "Up Right"
        AttackHeld := false
        Sleep 80
    }
}

ReleaseAll(*) {
    AttackOff()
    return 0
}

; ---------------------------------------------------------------- death
IsDead() {
    if (Cfg["Health.Color"] = "")
        return false
    c := PixelGetColor(N("Health.X"), N("Health.Y"))
    return !ColorNear(c, Cfg["Health.Color"], N("Health.Tolerance"))
}

OnDeath() {
    global DeathTimes, SpotIdx
    AttackOff()
    Log("персонаж убит (спот " SpotIdx ")")
    Status("убит, жду респаун")
    DeathTimes.Push(A_TickCount)
    windowMs := N("Spots.DeathWindowMin") * 60000
    recent := []
    for t in DeathTimes {
        if (A_TickCount - t <= windowMs)
            recent.Push(t)
    }
    DeathTimes := recent
    cnt := SpotCount()
    if (cnt > 1 && recent.Length >= N("Spots.DeathsToSwitch")) {
        SpotIdx := Mod(SpotIdx, cnt) + 1
        DeathTimes := []
        Log("слишком часто умираю — меняю спот на " SpotIdx)
    }
    if !Wait(N("Health.RespawnWait"))
        return
    deadline := A_TickCount + 30000
    while (Running && IsDead() && A_TickCount < deadline)
        Sleep 500
    GoToSpot()
}

; ---------------------------------------------------------------- spots
SpotCount() {
    n := 0
    Loop {
        if (IniRead(INI, "Spot" A_Index, "Steps", "") = "")
            break
        n++
    }
    return n
}

GoToSpot() {
    global SpotIdx
    cnt := SpotCount()
    if (cnt = 0) {
        Log("споты не заданы ([Spot1] Steps=...) — фармлю на месте")
        return false
    }
    if (SpotIdx > cnt)
        SpotIdx := 1
    name := IniRead(INI, "Spot" SpotIdx, "Name", "Spot" SpotIdx)
    Status("иду на спот " SpotIdx ": " name)
    Log("иду на спот " SpotIdx ": " name)
    AttackOff()
    ok := RunSteps(IniRead(INI, "Spot" SpotIdx, "Steps", ""))
    Status("фарм, спот " SpotIdx ": " name)
    return ok
}

; ---------------------------------------------------------------- disconnect
IsDisconnected() {
    win := Cfg["Game.Window"]
    if !WinExist(win)
        return true
    img := Cfg["Disconnect.Image"]
    if (img != "") {
        path := InStr(img, ":") ? img : A_ScriptDir "\" img
        if FileExist(path) {
            try {
                WinGetClientPos(, , &w, &h, win)
                if ImageSearch(&fx, &fy, 0, 0, w, h, "*40 " path) {
                    Log("найдено окно дисконнекта")
                    return true
                }
            }
        }
    }
    return false
}

Reconnect() {
    AttackOff()
    Status("дисконнект — перезаход")
    Log("перезаход в игру")
    if WinExist(Cfg["Game.Window"]) || ProcessExist(Cfg["Game.Exe"]) {
        ProcessClose(Cfg["Game.Exe"])
        if !Wait(3000)
            return false
    }
    launcher := Cfg["Game.Launcher"]
    if (launcher = "" || !FileExist(launcher)) {
        Log("Game.Launcher не задан или не найден: " launcher)
        return false
    }
    SplitPath launcher, , &dir
    Run('"' launcher '"', dir)
    if (Cfg["Game.LauncherSteps"] != "") {
        if !Wait(5000)
            return false
        RunSteps(Cfg["Game.LauncherSteps"])
    }
    if !WinWait(Cfg["Game.Window"], , N("Game.LauncherWait") // 1000) {
        Log("окно игры не появилось за " N("Game.LauncherWait") // 1000 " с")
        return false
    }
    WinActivate Cfg["Game.Window"]
    if !Wait(3000)
        return false
    if !RunSteps(Cfg["Game.LoginSteps"])
        return false
    if !WinExist(Cfg["Game.Window"])
        return false
    Log("вход выполнен")
    GoToSpot()
    return true
}

EnsureActive() {
    win := Cfg["Game.Window"]
    if WinExist(win) && !WinActive(win) {
        WinActivate win
        Sleep 300
    }
}

; ---------------------------------------------------------------- inventory
CellCenter(prefix, i) {
    cols := N(prefix ".Cols"), cw := N(prefix ".CellW"), ch := N(prefix ".CellH")
    r := i // cols, c := Mod(i, cols)
    return [N(prefix ".X") + c * cw + cw // 2, N(prefix ".Y") + r * ch + ch // 2]
}

GridTotal(prefix) => N(prefix ".Cols") * N(prefix ".Rows")

EmptyColors(prefix) {
    s := IniRead(INI, prefix "Empty", "Colors", "")
    return (s = "") ? [] : StrSplit(s, ",")
}

IsCellEmpty(prefix, i, empties) {
    p := CellCenter(prefix, i)
    c := PixelGetColor(p[1], p[2])
    return ColorNear(c, empties[i + 1], N(prefix ".Tolerance"))
}

CellMatches(prefix, i, dir) {
    if !DirExist(dir)
        return false
    cols := N(prefix ".Cols"), cw := N(prefix ".CellW"), ch := N(prefix ".CellH")
    r := i // cols, c := Mod(i, cols)
    x1 := N(prefix ".X") + c * cw - 3, y1 := N(prefix ".Y") + r * ch - 3
    x2 := x1 + cw + 6, y2 := y1 + ch + 6
    Loop Files dir "\*.png" {
        try {
            if ImageSearch(&fx, &fy, x1, y1, x2, y2, "*" N("Inventory.ImageTol") " " A_LoopFileFullPath)
                return true
        }
    }
    return false
}

OpenInv() {
    Send Cfg["Inventory.Key"]
    Sleep 500
}

; Выбрасывает мусор. Возвращает {free: свободных ячеек (-1 = не откалибровано), dropped: выброшено}
CleanInventory() {
    empties := EmptyColors("Inventory")
    total := GridTotal("Inventory")
    if (empties.Length < total) {
        Log("инвентарь не откалиброван — открой пустой инвентарь и нажми F11")
        return {free: -1, dropped: 0}
    }
    Status("чищу инвентарь")
    OpenInv()
    mode := StrLower(Cfg["Inventory.Mode"])
    keepDir := A_ScriptDir "\keep"
    junkDir := A_ScriptDir "\junk"
    dropped := 0
    skip := Map()
    Loop 3 {
        changed := false
        Loop total {
            if !Running
                break
            i := A_Index - 1
            if skip.Has(i) || IsCellEmpty("Inventory", i, empties)
                continue
            if (mode = "junk")
                isJunk := CellMatches("Inventory", i, junkDir)
            else
                isJunk := !CellMatches("Inventory", i, keepDir)
            if !isJunk {
                skip[i] := 1
                continue
            }
            p := CellCenter("Inventory", i)
            Click p[1] " " p[2]
            Sleep 300
            Click N("Inventory.DropX") " " N("Inventory.DropY")
            Sleep 500
            dropped++
            changed := true
        }
        if !changed
            break
    }
    free := 0
    Loop total {
        if IsCellEmpty("Inventory", A_Index - 1, empties)
            free++
    }
    OpenInv() ; повторное нажатие закрывает инвентарь
    Status("фарм, спот " SpotIdx)
    return {free: free, dropped: dropped}
}

StashTrip() {
    global Running
    AttackOff()
    Status("инвентарь полон — несу в сундук")
    Log("инвентарь полон — иду к сундуку")
    if (Cfg["Vault.Steps"] = "") {
        Log("Vault.Steps не заданы — сундук пропущен")
        return false
    }
    if !RunSteps(Cfg["Vault.Steps"])
        return false
    if !Wait(1000)
        return false
    invE := EmptyColors("Inventory"), vE := EmptyColors("Vault")
    total := GridTotal("Inventory"), vtotal := GridTotal("Vault")
    if (vE.Length < vtotal) {
        Log("сундук не откалиброван — открой пустой сундук и нажми Ctrl+F11")
        RunSteps(Cfg["Vault.CloseSteps"])
        return false
    }
    moved := 0
    vaultFull := false
    Loop total {
        if !Running
            break
        i := A_Index - 1
        if IsCellEmpty("Inventory", i, invE)
            continue
        p := CellCenter("Inventory", i)
        Click p[1] " " p[2]
        Sleep 300
        placed := false
        Loop vtotal {
            vi := A_Index - 1
            if !IsCellEmpty("Vault", vi, vE)
                continue
            q := CellCenter("Vault", vi)
            Click q[1] " " q[2]
            Sleep 350
            if !IsCellEmpty("Vault", vi, vE) {
                placed := true
                break
            }
        }
        if placed {
            moved++
        } else {
            Click p[1] " " p[2] ; вернуть вещь на место
            Sleep 300
            vaultFull := true
            Log("сундук полон")
            break
        }
    }
    Log("в сундук перенесено вещей: " moved)
    RunSteps(Cfg["Vault.CloseSteps"])
    Wait(800)
    if vaultFull {
        Log("сундук полон — бот остановлен")
        Running := false
        Status("сундук полон — остановлен")
        return false
    }
    return GoToSpot()
}

; ---------------------------------------------------------------- steps
; Шаги через ";":
;   click:x,y        — левый клик (координаты внутри окна игры)
;   rclick:x,y       — правый клик
;   walk:x,y,ms      — клик по земле и ждать ms
;   key:{F1}         — нажать клавишу (синтаксис Send)
;   type:текст       — напечатать текст ({LOGIN} и {PASS} подставляются)
;   cmd:/move arena  — Enter, текст, Enter (команда в чат)
;   wait:ms          — пауза
;   activate         — сделать окно игры активным
RunSteps(steps) {
    if (Trim(steps) = "")
        return true
    for raw in StrSplit(steps, ";") {
        s := Trim(raw)
        if (s = "")
            continue
        if !Running
            return false
        pos := InStr(s, ":")
        act := StrLower(pos ? SubStr(s, 1, pos - 1) : s)
        arg := pos ? SubStr(s, pos + 1) : ""
        switch act {
            case "click", "rclick", "walk":
                a := StrSplit(arg, ",")
                btn := (act = "rclick") ? " Right" : ""
                Click Trim(a[1]) " " Trim(a[2]) btn
                pause := (act = "walk" && a.Length >= 3) ? Integer(Trim(a[3])) : 300
                if !Wait(pause)
                    return false
            case "key":
                Send arg
                Sleep 200
            case "type":
                arg := StrReplace(arg, "{LOGIN}", Cfg["Game.Login"])
                arg := StrReplace(arg, "{PASS}", Cfg["Game.Password"])
                SendText arg
                Sleep 200
            case "cmd":
                Send "{Enter}"
                Sleep 250
                SendText arg
                Sleep 150
                Send "{Enter}"
                Sleep 500
            case "wait":
                if !Wait(Integer(Trim(arg)))
                    return false
            case "activate":
                if WinExist(Cfg["Game.Window"])
                    WinActivate Cfg["Game.Window"]
                Sleep 300
            default:
                Log("неизвестный шаг: " s)
        }
    }
    return true
}

; ---------------------------------------------------------------- calibration
ToggleCalib() {
    global CalibOn
    CalibOn := !CalibOn
    if CalibOn {
        SetTimer(CalibTick, 100)
    } else {
        SetTimer(CalibTick, 0)
        ToolTip(, , , 2)
    }
}

CalibTick() {
    MouseGetPos &x, &y
    c := PixelGetColor(x, y)
    ToolTip("x=" x "  y=" y "  " c "`nCtrl+Alt+C — скопировать", , , 2)
}

CopyPoint() {
    MouseGetPos &x, &y
    c := PixelGetColor(x, y)
    A_Clipboard := x "," y " " c
    ToolTip("скопировано: " x "," y " " c, , , 3)
    SetTimer(() => ToolTip(, , , 3), -1500)
}

RecordGrid(prefix) {
    LoadCfg()
    if !WinExist(Cfg["Game.Window"]) {
        MsgBox "Окно игры не найдено: " Cfg["Game.Window"]
        return
    }
    WinActivate Cfg["Game.Window"]
    Sleep 400
    if (N(prefix ".CellW") <= 0 || (N(prefix ".X") = 0 && N(prefix ".Y") = 0)) {
        MsgBox "Сначала задай " prefix ".X / Y / CellW / CellH в settings.ini"
        return
    }
    s := ""
    Loop GridTotal(prefix) {
        p := CellCenter(prefix, A_Index - 1)
        s .= (s = "" ? "" : ",") PixelGetColor(p[1], p[2])
    }
    IniWrite(s, INI, prefix "Empty", "Colors")
    MsgBox "Записаны цвета пустых ячеек: " prefix " (" GridTotal(prefix) " шт.)"
}

CheckGrid(prefix) {
    LoadCfg()
    if !WinExist(Cfg["Game.Window"])
        return
    WinActivate Cfg["Game.Window"]
    Sleep 300
    empties := EmptyColors(prefix)
    Loop GridTotal(prefix) {
        i := A_Index - 1
        p := CellCenter(prefix, i)
        MouseMove p[1], p[2]
        st := (empties.Length > i) ? (IsCellEmpty(prefix, i, empties) ? "пусто" : "ЗАНЯТО") : "не откалибровано"
        ToolTip(prefix " #" i ": " st, , , 2)
        Sleep 120
    }
    ToolTip(, , , 2)
}

; ---------------------------------------------------------------- utils
ColorNear(a, b, tol) {
    a := Integer(a), b := Integer(b)
    return Abs(((a >> 16) & 0xFF) - ((b >> 16) & 0xFF)) <= tol
        && Abs(((a >> 8) & 0xFF) - ((b >> 8) & 0xFF)) <= tol
        && Abs((a & 0xFF) - (b & 0xFF)) <= tol
}

Wait(ms) {
    end := A_TickCount + ms
    while (A_TickCount < end) {
        if !Running
            return false
        Sleep 50
    }
    return true
}

Status(msg) {
    ToolTip("Murdrum bot: " msg, 10, 10, 1)
}

Log(msg) {
    try FileAppend(FormatTime(, "yyyy-MM-dd HH:mm:ss") "  " msg "`n", LOGFILE, "UTF-8")
}
