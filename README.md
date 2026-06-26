````md
<p align="center">
  <img src="assets/icon.png" width="130" alt="SODB Save Editor icon">
</p>

<h1 align="center">Shadows of Doubt — SODB Save Editor</h1>

<p align="center">
  <b>Unofficial save editor, viewer and investigation toolkit for <i>Shadows of Doubt</i>.</b>
  <br>
  Распаковывай <code>.sodb</code>, смотри кейсы, людей, пароли, убийц, предметы, адреса и редактируй сейв через удобный GUI.
</p>

<p align="center">
  <a href="https://github.com/ambiguoustrader/SODB-Save-Editor/releases/latest">
    <img src="https://img.shields.io/badge/Download-Latest%20Release-2ea44f?style=for-the-badge&logo=github" alt="Download latest release">
  </a>
</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Windows" src="https://img.shields.io/badge/Windows-10%2F11-0078D6?logo=windows&logoColor=white">
  <img alt="GUI" src="https://img.shields.io/badge/GUI-tkinter-blueviolet">
  <img alt="Save format" src="https://img.shields.io/badge/save-.sodb-orange">
  <img alt="Status" src="https://img.shields.io/badge/status-fan_tool-red">
</p>

---

## Overview

**SODB Save Editor** — это неофициальная GUI-утилита для сохранений **Shadows of Doubt**.

Она позволяет открыть `.sodb` сейв, распаковать его, посмотреть внутренние игровые данные в нормальном виде и при необходимости аккуратно отредактировать отдельные значения.

Примерно так:

```text
.sodb save  →  decompress  →  JSON  →  view / search / edit  →  save back
````

Главная идея проекта — не заставлять копаться в огромном JSON вручную, а дать удобный интерфейс для:

* просмотра активных дел;
* поиска убийц и криминалов;
* просмотра жителей города;
* поиска паролей;
* отслеживания предметов;
* просмотра комнат, адресов и компаний;
* редактирования базовых параметров игрока.

> [!WARNING]
> Это **неофициальный фанатский инструмент**.
> Проект не связан с ColePowered Games, Fireshine Games, Steam или Valve.
> Перед редактированием сейва всегда делайте бэкап.

---

## Download

Скачать последнюю версию:

**https://github.com/ambiguoustrader/SODB-Save-Editor/releases/latest**

README ведёт на `/releases/latest`, поэтому ссылку не нужно менять после каждого нового релиза.

---

## Key Features

### Save Management

* открытие `.sodb` файлов;
* поддержка Brotli-сжатых сохранений;
* поддержка распакованных JSON-сейвов;
* экспорт распакованного JSON;
* просмотр Raw JSON внутри приложения;
* сохранение обратно в `.sodb`;
* автобэкапы с историей;
* восстановление из бэкапов;
* валидатор сейва;
* проверка roundtrip encode/decode;
* безопасная перезапись через `Backup + overwrite`.

---

### Player Editor

Можно редактировать базовые параметры игрока:

* money;
* lockpicks;
* Social Credit;
* health;
* nourishment;
* hydration;
* energy;
* hygiene.

Также есть справка по значениям прямо внутри приложения.

---

### Cases

Вкладка дел показывает:

* активные кейсы;
* цели дел;
* жертв;
* убийц;
* оружие;
* места;
* связанные HumanID / RoomID / EvidenceID;
* готовые ответы и resolveQuestions, если они есть в сейве.

---

### Citizens

Вкладка жителей позволяет смотреть:

* всех citizens в городе;
* HumanID;
* имена;
* адреса;
* работу;
* компании;
* связи;
* координаты;
* пароли, если они есть в сейве;
* полную карточку человека.

---

### Passwords

Отдельная вкладка для `passcodes`.

Можно искать по:

* имени;
* фамилии;
* HumanID;
* коду;
* RoomID;
* адресу.

Важно: вкладка показывает только те пароли, которые реально есть в сейве.
Если пароль человека ещё не сгенерирован или не сохранён в `passcodes`, редактор не сможет честно показать его как найденный.

---

### Items / Interactables

Можно искать предметы и смотреть, где они находятся:

* item ID;
* название / preset;
* владелец;
* RoomID;
* адрес;
* этаж;
* координаты X / Y / Z;
* locked state;
* passcode state.

Подходит для поиска предметов вроде:

```text
Envelope
SealedEnvelope
SniperRifle
Key
Note
Vmail
```

---

### Rooms and Addresses

Есть адресная книга:

* RoomID;
* LocationID;
* адрес;
* этаж;
* жильцы;
* компании;
* связанные предметы;
* пароли комнат.

---

### Companies and Jobs

Отдельная вкладка для компаний и рабочих мест:

* компании;
* сотрудники;
* должности;
* boss / worker links;
* связанные комнаты;
* рабочие места;
* CompanyRoster.

---

### CSV Export

Таблицы можно экспортировать в CSV:

* citizens;
* passcodes;
* cases;
* items;
* rooms;
* companies;
* evidence / interactables;
* item tracker results.

---

## Detective Mode

По умолчанию приложение открывается как обычный save editor.
**Detective Mode** выключен по умолчанию, чтобы не перегружать интерфейс.

Его можно включить отдельно, когда нужны расследовательские инструменты.

Detective Mode добавляет:

* глобальный поиск по сейву;
* карточку NPC;
* timeline убийств;
* карту / heatmap;
* граф связей;
* трекер предметов;
* фильтр предметов по этажу;
* выделение выбранного предмета на карте;
* Sync Disk manager;
* инвентарь;
* городскую статистику;
* JSON Inspector.

Горячие клавиши:

```text
Ctrl+Shift+F
Ctrl+P
```

---

## Item Tracker

Одна из главных функций Detective Mode.

Трекер предметов позволяет найти объект в городе и показать его на 2D-карте.

Можно:

* оставить пустой фильтр и увидеть все предметы;
* ввести название предмета;
* отфильтровать по этажу;
* выбрать конкретный предмет;
* включить режим “только выбранный”;
* скопировать координаты;
* экспортировать результат в CSV.

Пример:

```text
Envelope
```

Редактор покажет, где находится конверт:

```text
item id
preset
room
address
floor
x / y / z
owner
```

---

## Sync Disk Manager

В Detective Mode есть управление Sync Disks:

* просмотр установленных Sync Disks;
* добавление одного SyncDisk;
* добавление всех отсутствующих SyncDisks;
* удаление выбранного SyncDisk;
* редактирование `state`;
* редактирование `level`.

---

## Global Search

Глобальный поиск ищет сразу по нескольким сущностям:

* citizens;
* cases;
* passcodes;
* companies;
* rooms;
* items;
* evidence;
* interactables;
* murders;
* JSON fields.

Это удобно, если известен только кусок имени, HumanID, RoomID, предмет или пароль.

---

## Save Location

Обычно сохранения находятся здесь:

```text
%USERPROFILE%\AppData\LocalLow\ColePowered Games\Shadows of Doubt\Save
```

Перед сохранением изменений лучше закрыть игру.

---

## Installation

### Option 1 — Download EXE

Скачайте последнюю версию:

```text
https://github.com/ambiguoustrader/SODB-Save-Editor/releases/latest
```

Запустите:

```text
SODB_Save_Editor.exe
```

Python для EXE-версии не нужен.

---

### Option 2 — Run from Source

Требования:

* Windows 10/11;
* Python 3.10+.

Установка зависимостей:

```bat
python -m pip install -r requirements.txt
```

Запуск:

```bat
python sod_save_editor.py
```

Или:

```bat
run_sod_save_editor.bat
```

---

## Build EXE

Для сборки одного `.exe`:

```bat
build_exe.bat
```

Готовый файл появится здесь:

```text
dist\SODB_Save_Editor.exe
```

Если рядом лежит `icon.ico`, он будет использован как иконка приложения.

---

## Repository Structure

```text
.
├── .github/workflows/build-windows.yml
├── assets/
│   └── icon.png
├── docs/
│   └── SAVE_FORMAT.md
├── build_exe.bat
├── icon.ico
├── requirements.txt
├── requirements-dev.txt
├── run_sod_save_editor.bat
└── sod_save_editor.py
```

---

## Safety Notes

* Не редактируйте сейв, пока он открыт игрой.
* Перед перезаписью используйте бэкап.
* Автобэкапы создаются автоматически.
* Не открывайте сейвы из непроверенных источников.
* Структура `.sodb` может измениться после обновлений игры.
* Если игра не видит изменённый сейв, восстановите предыдущий бэкап.

---

## FAQ

### Does it edit the save automatically?

No.
Изменения сохраняются только после явного нажатия кнопки сохранения.

### Does it create backups?

Yes.
Перед перезаписью создаётся timestamped backup.

### Can it show every password?

It shows passcodes that exist in the save.
Если пароль не был сгенерирован или не лежит в `passcodes`, редактор не сможет честно показать его как найденный.

### Can it find the murderer?

Yes, if murder data is present in the save.
Редактор ищет данные через murder/case/evidence structures.

### Can it locate items?

Yes.
Detective Mode can show item coordinates, room, floor and map position.

### Is Detective Mode always enabled?

No.
Обычный режим редактора остаётся основным. Detective Mode включается отдельно.

---

## Original Game

* **Steam:** https://store.steampowered.com/app/986130/Shadows_of_Doubt/
* **Official page:** https://colepowered.com/games/shadows-of-doubt/
* **Developer:** https://colepowered.com/
* **Publisher:** https://fireshinegames.co.uk/games/

---

## Disclaimer

This is an unofficial fan-made tool.

It is not affiliated with:

* ColePowered Games;
* Fireshine Games;
* Steam;
* Valve.

All trademarks, names, game data and related materials belong to their respective owners.

---

## License

See [`LICENSE`](LICENSE).

```
```
