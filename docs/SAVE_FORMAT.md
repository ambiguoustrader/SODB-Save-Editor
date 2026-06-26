# Формат сохранений `.sodb`

Эти заметки основаны на практическом разборе сейвов и могут быть неполными.

## Кодирование

Сейв может быть в одном из двух видов:

1. **Plain JSON** — если в игре отключено сжатие сохранений.
2. **Brotli + 4-byte trailer** — обычный `.sodb` файл.

Для сжатого `.sodb` наблюдаемая структура такая:

```text
[ brotli-compressed JSON bytes ][ 4 bytes: expected decompressed size, little-endian ]
```

Минимальная распаковка:

```python
from pathlib import Path
import brotli

src = Path("save.sodb")
data = src.read_bytes()
expected_size = int.from_bytes(data[-4:], "little")
raw_json = brotli.decompress(data[:-4])

print(len(raw_json), expected_size)
Path("save.json").write_bytes(raw_json)
```

## Поля игрока

Часто встречаются:

```text
money
lockpicks
socCredit
health
nourishment
hydration
energy
hygiene
```

Для `health / nourishment / hydration / energy / hygiene` безопаснее держаться диапазона `0.0–1.0`. Для `money / lockpicks / socCredit` используются целые числа.

## Люди

Жители обычно сопоставляются по `HumanID` / строкам вида:

```text
Human229
Human57
```

Часть данных о человеке может находиться в разных местах сейва: citizens, evidence, resident roster, company roster, ties/sightings.

## Пароли

В `passcodes` обычно лежат уже известные/сгенерированные/сохранённые игрой коды.

```text
type=0  личный код человека, id = HumanID
type=1  код комнаты/локации, id = RoomID или похожий ID
```

Важно: отсутствие `type=0` для конкретного HumanID не доказывает, что у NPC нет пароля. Это означает только то, что такой passcode не найден в текущем списке `passcodes` сейва.

## Убийцы и криминалы

Полезные поля/структуры, которые встречаются в сейвах:

```text
currentMurderer
currentVictim
murdererID
iaMurders
killer
victim
chosenMO
murderPreset
weapon
convicted
death.killer
```

Названия и структура могут меняться между версиями игры.
