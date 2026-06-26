# Changelog

## Unreleased

## v9 — Detective item tracker

- Added Detective Mode → **Трекер предметов**.
- Tracks interactables by exact world coordinates (`wPos`/`spWPos`) and shows X/Y/Z, room, address hint, owner and floor group.
- Added floor filter, item search, selected-item highlighting, selected-only map mode, coordinate copy and CSV export.
- The map shows all filtered items by default; selecting an item highlights it precisely on the X/Z map.


## v8 — Layout + SyncDisk installer

- Improved TreeView layout: all major tables now get horizontal and vertical scrollbars.
- Changed auto-fit behavior so wide columns are not compressed into unreadable widths; horizontal scrolling is preferred.
- Added SyncDisk catalog with all 18 known internal save codes.
- Added SyncDisk installer in Detective Mode: add one disk, add all missing disks, delete selected disk, edit state/level.
- SyncDisk table now shows both internal code and readable display name.

- Added save validator tab with JSON shape checks, passcodes checks, room/citizen reference checks and encode/decode roundtrip.
- Added timestamped autobackups in `backups/<save_name>/` before overwrite and restore support from the GUI.
- Added full person card with work/company hints, home rooms, location, passcode, related cases and murder/crime links.
- Expanded case details with ready-answer summary, mapped resolve questions, job location and IA murder data.
- Added item locator tab for interactables: owner, room, address, coordinates, locked/passcode and evidence key.
- Added address book tab for RoomID → LocationID/address hints, occupants, companies, room passwords and item counts.
- Added horizontal scrollbars and automatic TreeView column sizing for better window/table layout.

- Added relation graph tab for citizens.
- Added CSV export for visible tables.
- Added advanced evidence/interactables search tab.
- Added RoomID → address hints where the save exposes this mapping.
- Added companies/workplaces tab based on CompanyRoster and companies sales.
- Removed personal/user-specific references from runtime app ID.
- Added runtime window/taskbar icon loading for source and PyInstaller one-file EXE.
- Bundled `icon.ico` into the EXE build with `--add-data`.

## v4

- Добавлена вкладка «Пароли».
- Добавлен поиск passcode по имени, HumanID, коду и RoomID.
- Добавлена возможность задать личный пароль для HumanID.
- В «Люди / связи» добавлена колонка password.
- Добавлены кнопки «Очистить», «Сбросить правки» и «?».
- Улучшена сборка EXE через `python -m PyInstaller`.

## v3

- Добавлены очистка состояния и сброс правок.
- Добавлена встроенная справка по диапазонам статов.

## v2

- Добавлена вкладка паролей.
- Добавлены операции копирования/изменения кода.

## v1

- Первый GUI-прототип: открытие `.sodb`, распаковка, просмотр кейсов/людей/убийц, raw JSON, редактирование базовых статов.

## v7 — Optional Detective Mode

- Added a switchable **Detective Mode** tab, hidden by default so the editor view stays clean.
- Added global search across citizens, cases, passcodes, companies, items, rooms, evidence and interactables.
- Added murder timeline with killer, victim, weapon, address and raw data preview.
- Added lightweight 2D map / heatmap from save coordinates.
- Added inventory viewer/editor for carried interactable.
- Added Sync Disk viewer with level/state editing.
- Added apartments/addresses view with residents, rooms and passcodes.
- Added city statistics and top interactable presets.
- Added JSON Inspector for top-level browsing and path/value search.
- Improved relation graph visuals with relation color legend.
- Detective Mode can be opened with the toolbar toggle, Ctrl+Shift+F, or Ctrl+P.
