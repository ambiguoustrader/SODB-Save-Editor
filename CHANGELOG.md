# Changelog

## Unreleased

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
