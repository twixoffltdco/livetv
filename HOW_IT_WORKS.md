# Как работает LiveM3U 🔍

## Архитектура проекта

LiveM3U вдохновлён лучшими практиками из проектов:
- **[iptv-org/iptv](https://github.com/iptv-org/iptv)** - крупнейшая коллекция IPTV каналов
- **[zabava-project](https://github.com/CrocoUser/zabava-project)** - автоматический поиск потоков
- **[IPTVPlay](https://github.com/blackbirdstudiorus/IPTVPlay)** - плеер с автообновлением плейлистов
- **[anthonyaxenov/iptv](https://github.com/anthonyaxenov/iptv)** - русскоязычные IPTV плейлисты

## Принцип работы

### 1️⃣ Поиск потоков (StreamFinder)

Бот **НЕ использует заранее заданные URL**. Вместо этого он:

```python
# Загружает плейлист из iptv-org
IPTV_ORG_PLAYLIST = "https://iptv-org.github.io/iptv/languages/rus.m3u"

# Дополнительные источники
additional_playlists = [
    "https://iptv-org.github.io/iptv/countries/ru.m3u",
    "https://iptv-org.github.io/iptv/categories/news.m3u",
    "https://iptv-org.github.io/iptv/categories/movies.m3u",
]
```

**Парсинг M3U:**
- Извлекает название канала из `#EXTINF:-1 tvg-name="...",group-title="...",Название`
- Извлекает URL потока из следующей строки
- Сохраняет категорию, логотип, группу

### 2️⃣ Проверка потоков (StreamChecker)

Каждый найденный поток проверяется на работоспособность:

```python
# 1. HEAD запрос для быстрой проверки
response = session.head(url, timeout=5)

# 2. Проверка Content-Type
if 'video' in content_type or 'mpegurl' in content_type:
    status = "working"

# 3. Если HEAD не сработал - GET запрос с чтением первых байт
response = session.get(url, stream=True)
first_bytes = response.raw.read(1024)
if '#extm3u' in first_bytes or '<mpd' in first_bytes:
    status = "working"
```

**Фильтрация невалидных URL:**
- Пропускаются iframe и HTML страницы (`player.smotrim`, `/watch/`, `.html`)
- Пропускаются не-http ссылки

### 3️⃣ Генерация плейлиста (M3UPlaylist)

Создаётся итоговый M3U файл только с рабочими потоками:

```m3u
#EXTM3U
# Обновлён: 2024-01-15 10:30:00
# Всего каналов: 150

# Федеральные
#EXTINF:-1 tvg-logo="..." group-title="Федеральные", Первый канал
https://рабочий-url.ru/playlist.m3u8

# Новости
#EXTINF:-1 tvg-logo="..." group-title="Новости", Россия 24
https://рабочий-url.ru/stream.m3u8
```

## Отличия от старой версии

### ❌ БЫЛО (неправильно):
```json
{
  "name": "Первый канал",
  "urls": [
    "https://streaming.televizor-24.ru/channels/1.m3u8",  // URL устаревает
    "https://edge1.1cliptv.com/dash-live2/streams/1ch/1ch.mpd"  // может не работать
  ]
}
```
**Проблемы:**
- URL быстро устаревают
- Нужна ручная поддержка списка
- Потоки перестают работать без уведомления

### ✅ СТАЛО (правильно):
```json
{
  "name": "Первый канал",
  "category": "Федеральные",
  "search_terms": ["первый канал", "1tv"]
}
```
**Преимущества:**
- Бот сам ищет актуальные URL в iptv-org
- Автоматически находит новые рабочие потоки
- Не нужно вручную обновлять список URL

## Как это связано с iptv-org?

[iptv-org/iptv](https://github.com/iptv-org/iptv) - это проект, который:
1. Собирает publicly available IPTV каналы со всего мира
2. Проверяет их работоспособность автоматически
3. Поддерживает актуальные плейлисты по языкам, странам, категориям

**LiveM3U использует этот подход:**
- Загружает готовые плейлисты от iptv-org
- Дополнительно проверяет каждый поток
- Фильтрует только рабочие для вашего региона
- Создаёт персональный плейлист

## Структура классов

```
┌─────────────────┐
│  StreamFinder   │  ← Поиск в iptv-org и других источниках
├─────────────────┤
│ - fetch_iptv_org_playlist()
│ - search_additional_sources()
│ - find_streams()
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  StreamChecker  │  ← Проверка доступности каждого URL
├─────────────────┤
│ - check_stream()
│ - анализ Content-Type
│ - проверка сигнатур M3U8/MPD
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  M3UPlaylist    │  ← Генерация итогового плейлиста
├─────────────────┤
│ - generate_m3u()
│ - фильтрация working streams
│ - группировка по категориям
└────────┬────────┘
         │
         ▼
    playlist.m3u
```

## Запуск

```bash
# Однократная проверка
python src/bot.py --once

# Непрерывный режим (проверка каждый час)
python src/bot.py --interval 3600

# С увеличенным количеством параллельных проверок
python src/bot.py --workers 30
```

## Результат работы

В папке `data/` создаются:

1. **playlist.m3u** - основной плейлист с рабочими каналами
2. **playlist.m3u8** - HLS-версия плейлиста
3. **statistics.json** - статистика:
   ```json
   {
     "total_streams": 1705,
     "working_streams": 350,
     "dead_streams": 1355,
     "categories": {
       "Новости": {"total": 50, "working": 15},
       "Федеральные": {"total": 30, "working": 10}
     }
   }
   ```

## Почему это лучше?

| Характеристика | Старый подход | Новый подход (LiveM3U) |
|---------------|--------------|------------------------|
| Источник URL | Ручной список | Автоматически из iptv-org |
| Актуальность | Устаревает | Всегда свежий |
| Поддержка | Ручная | Автоматическая |
| Надёжность | Низкая | Высокая |
| Масштабируемость | Ограничена | Неограниченно |

---

**LiveM3U** - это умный агрегатор, который использует лучшие практики сообщества IPTV для создания актуальных плейлистов! 🚀
