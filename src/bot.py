#!/usr/bin/env python3
"""
LiveM3U - Автоматический поисковой робот для создания актуальных M3U плейлистов
Сам ищет рабочие потоки через API iptv-org и проверку доступных источников
Вдохновлено проектами: iptv-org, zabava-project, IPTVPlay
"""

import os
import re
import json
import time
import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs

# Конфигурация
BASE_DIR = Path(__file__).parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

# Создаем директории
for directory in [CONFIG_DIR, DATA_DIR, LOGS_DIR]:
    directory.mkdir(exist_ok=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOGS_DIR / "livem3u.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class StreamInfo:
    """Информация о потоке"""
    name: str
    url: str
    category: str = "Общее"
    country: str = "RU"
    language: str = "rus"
    logo: str = ""
    group_title: str = ""
    last_checked: str = ""
    status: str = "unknown"  # working, dead, unknown

    def to_m3u_line(self) -> str:
        """Преобразовать в формат M3U"""
        logo_attr = f' tvg-logo="{self.logo}"' if self.logo else ""
        group_attr = f' group-title="{self.group_title}"' if self.group_title else ""
        return f'#EXTINF:-1{logo_attr}{group_attr}, {self.name}\n{self.url}'


class StreamChecker:
    """Проверка работоспособности потоков"""
    
    def __init__(self, timeout: int = 5, max_retries: int = 2):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': 'https://github.com/iptv-org/iptv',
            'Connection': 'keep-alive',
            'DNT': '1'
        })
    
    def check_stream(self, stream: StreamInfo) -> StreamInfo:
        """Проверить один поток"""
        # Пропускаем не-http ссылки и страницы плееров
        if not stream.url.startswith(('http://', 'https://')):
            stream.status = "dead"
            stream.last_checked = datetime.now().isoformat()
            return stream
        
        # Пропускаем iframe и html страницы
        if any(x in stream.url for x in ['iframe', 'player.smotrim', '/watch/', '.html']):
            stream.status = "dead"
            stream.last_checked = datetime.now().isoformat()
            return stream
        
        for attempt in range(self.max_retries):
            try:
                # Сначала пробуем HEAD запрос
                response = self.session.head(
                    stream.url, 
                    timeout=self.timeout,
                    allow_redirects=True
                )
                
                if response.status_code == 200:
                    # Проверяем Content-Type для видео потоков
                    content_type = response.headers.get('Content-Type', '').lower()
                    if any(x in content_type for x in ['video', 'mpegurl', 'mpd', 'octet-stream']):
                        stream.status = "working"
                        stream.last_checked = datetime.now().isoformat()
                        logger.info(f"✓ Рабочий поток: {stream.name}")
                        return stream
                
                # Если HEAD не сработал, пробуем GET с ограничением
                response = self.session.get(
                    stream.url, 
                    timeout=self.timeout,
                    allow_redirects=True,
                    stream=True
                )
                
                if response.status_code == 200:
                    # Проверяем первые байты на наличие M3U8 или MPD сигнатур
                    first_bytes = response.raw.read(1024).decode('utf-8', errors='ignore').lower()
                    if '#extm3u' in first_bytes or '<mpd' in first_bytes:
                        stream.status = "working"
                        stream.last_checked = datetime.now().isoformat()
                        logger.info(f"✓ Рабочий поток: {stream.name}")
                        return stream
                
                if response.status_code in [403, 404, 410]:
                    stream.status = "dead"
                    stream.last_checked = datetime.now().isoformat()
                    logger.debug(f"✗ Мёртвый поток ({response.status_code}): {stream.name}")
                    return stream
                    
            except requests.exceptions.RequestException as e:
                if attempt == self.max_retries - 1:
                    stream.status = "dead"
                    stream.last_checked = datetime.now().isoformat()
                    logger.debug(f"✗ Ошибка проверки: {stream.name} - {str(e)[:50]}")
                else:
                    time.sleep(0.5)
        
        stream.last_checked = datetime.now().isoformat()
        return stream


class StreamFinder:
    """Поисковой робот для автоматического нахождения потоков"""
    
    # Источники для поиска потоков (без конкретных URL - только API и каталоги)
    IPTV_ORG_API = "https://iptv-org.github.io/api"
    IPTV_ORG_PLAYLIST = "https://iptv-org.github.io/iptv/languages/rus.m3u"
    
    def __init__(self):
        self.channels_config = self._load_channels_config()
        self.checker = StreamChecker()
        self.found_urls: Set[str] = set()
    
    def _load_channels_config(self) -> List[Dict]:
        """Загрузить список каналов для поиска"""
        config_file = CONFIG_DIR / "channels.json"
        if config_file.exists():
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        
        # Список каналов для поиска (без URL!)
        default_channels = [
            {"name": "Первый канал", "category": "Федеральные", "search_terms": ["первый канал", "1tv"]},
            {"name": "Россия 1", "category": "Федеральные", "search_terms": ["россия 1", "russia1"]},
            {"name": "НТВ", "category": "Федеральные", "search_terms": ["нтв", "ntv"]},
            {"name": "ТНТ", "category": "Развлекательные", "search_terms": ["тнт", "tnt"]},
            {"name": "РЕН ТВ", "category": "Федеральные", "search_terms": ["рен тв", "rentv"]},
            {"name": "СТС", "category": "Развлекательные", "search_terms": ["стс", "ctc"]},
            {"name": "Домашний", "category": "Развлекательные", "search_terms": ["домашний", "domashniy"]},
            {"name": "ТВ-3", "category": "Развлекательные", "search_terms": ["тв3", "tv3"]},
            {"name": "Пятница!", "category": "Развлекательные", "search_terms": ["пятница", "pyatnitsa"]},
            {"name": "Звезда", "category": "Федеральные", "search_terms": ["звезда", "tvzvezda"]},
            {"name": "Мир", "category": "Федеральные", "search_terms": ["мир", "mir"]},
            {"name": "ТВ Центр", "category": "Федеральные", "search_terms": ["тв центр", "tvcenter"]},
            {"name": "Россия 24", "category": "Новости", "search_terms": ["россия 24", "russia24"]},
            {"name": "Карусель", "category": "Детские", "search_terms": ["карусель", "karusel"]},
            {"name": "ОТР", "category": "Федеральные", "search_terms": ["отр", "otr"]},
            {"name": "ТВ Культура", "category": "Культура", "search_terms": ["культура", "kultura"]},
            {"name": "Матч ТВ", "category": "Спорт", "search_terms": ["матч тв", "matchtv"]},
            {"name": "Москва 24", "category": "Региональные", "search_terms": ["москва 24", "moscow24"]},
            {"name": "RT News", "category": "Новости", "search_terms": ["rt news", "rt russian"]},
            {"name": "Euronews Russian", "category": "Новости", "search_terms": ["euronews russian", "euronews ru"]},
        ]
        
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(default_channels, f, ensure_ascii=False, indent=2)
        
        return default_channels
    
    def fetch_iptv_org_playlist(self) -> List[StreamInfo]:
        """Загрузить плейлист из iptv-org (основной источник как в iptv-org/iptv)"""
        streams = []
        logger.info("Загружаю плейлист от iptv-org...")
        
        try:
            response = requests.get(
                self.IPTV_ORG_PLAYLIST,
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=30
            )
            response.raise_for_status()
            
            lines = response.text.split('\n')
            current_name = ""
            current_group = ""
            current_logo = ""
            
            for line in lines:
                line = line.strip()
                if line.startswith('#EXTINF:'):
                    # Извлекаем информацию из строки EXTINF
                    if 'tvg-name=' in line:
                        match = re.search(r'tvg-name="([^"]*)"', line)
                        if match:
                            current_name = match.group(1)
                    
                    if 'group-title=' in line:
                        match = re.search(r'group-title="([^"]*)"', line)
                        if match:
                            current_group = match.group(1)
                    
                    if 'tvg-logo=' in line:
                        match = re.search(r'tvg-logo="([^"]*)"', line)
                        if match:
                            current_logo = match.group(1)
                    
                    # Извлекаем название канала после запятой
                    if ',' in line:
                        name_part = line.split(',')[-1].strip()
                        if name_part and not name_part.startswith('#'):
                            current_name = name_part
                
                elif line.startswith('http'):
                    # Это URL потока
                    if current_name and line not in self.found_urls:
                        self.found_urls.add(line)
                        stream = StreamInfo(
                            name=current_name,
                            url=line,
                            category=current_group or "Общее",
                            country="RU",
                            language="rus",
                            logo=current_logo,
                            group_title=current_group or "Общее"
                        )
                        streams.append(stream)
                        logger.debug(f"Найден канал: {current_name}")
                    
                    # Сбрасываем для следующего канала
                    current_name = ""
                    current_group = ""
                    current_logo = ""
            
            logger.info(f"Загружено {len(streams)} каналов из iptv-org")
            
        except Exception as e:
            logger.error(f"Ошибка загрузки iptv-org: {e}")
        
        return streams
    
    def search_additional_sources(self) -> List[StreamInfo]:
        """Поиск дополнительных источников через известные каталоги"""
        streams = []
        
        # Дополнительные источники в стиле iptv-org
        additional_playlists = [
            "https://iptv-org.github.io/iptv/countries/ru.m3u",
            "https://iptv-org.github.io/iptv/categories/news.m3u",
            "https://iptv-org.github.io/iptv/categories/movies.m3u",
        ]
        
        for playlist_url in additional_playlists:
            try:
                logger.info(f"Проверяю дополнительный источник: {playlist_url}")
                response = requests.get(playlist_url, timeout=15)
                
                if response.status_code == 200:
                    lines = response.text.split('\n')
                    current_name = ""
                    current_group = ""
                    
                    for line in lines:
                        line = line.strip()
                        if line.startswith('#EXTINF:'):
                            if 'group-title=' in line:
                                match = re.search(r'group-title="([^"]*)"', line)
                                if match:
                                    current_group = match.group(1)
                            if ',' in line:
                                name_part = line.split(',')[-1].strip()
                                if name_part:
                                    current_name = name_part
                        
                        elif line.startswith('http') and current_name:
                            if line not in self.found_urls:
                                self.found_urls.add(line)
                                stream = StreamInfo(
                                    name=current_name,
                                    url=line,
                                    category=current_group or "Общее",
                                    country="RU",
                                    language="rus",
                                    group_title=current_group or "Общее"
                                )
                                streams.append(stream)
                            
                            current_name = ""
                            current_group = ""
                            
            except Exception as e:
                logger.debug(f"Не удалось загрузить {playlist_url}: {e}")
        
        logger.info(f"Найдено дополнительно {len(streams)} каналов")
        return streams
    
    def find_streams(self) -> List[StreamInfo]:
        """Основной метод поиска потоков"""
        logger.info("=" * 50)
        logger.info("Запуск поискового робота LiveM3U")
        logger.info("=" * 50)
        
        all_streams = []
        
        # 1. Загружаем из основного источника iptv-org
        iptv_streams = self.fetch_iptv_org_playlist()
        all_streams.extend(iptv_streams)
        
        # 2. Ищем в дополнительных источниках
        additional_streams = self.search_additional_sources()
        all_streams.extend(additional_streams)
        
        # Удаляем дубликаты по URL
        unique_streams = {}
        for stream in all_streams:
            if stream.url not in unique_streams:
                unique_streams[stream.url] = stream
        
        logger.info(f"Всего найдено уникальных потоков: {len(unique_streams)}")
        return list(unique_streams.values())
    
    def check_all_streams(self, streams: List[StreamInfo], max_workers: int = 20) -> List[StreamInfo]:
        """Проверить все потоки параллельно"""
        logger.info(f"Проверяю работоспособность потоков ({max_workers} одновременных проверок)...")
        checked_streams = []
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.checker.check_stream, stream): stream for stream in streams}
            
            completed = 0
            for future in as_completed(futures):
                try:
                    result = future.result()
                    checked_streams.append(result)
                    completed += 1
                    
                    if completed % 50 == 0:
                        working = sum(1 for s in checked_streams if s.status == "working")
                        logger.info(f"Проверено {completed}/{len(streams)}, рабочих: {working}")
                        
                except Exception as e:
                    logger.error(f"Ошибка при проверке: {e}")
        
        working_count = sum(1 for s in checked_streams if s.status == "working")
        logger.info(f"✓ Проверка завершена. Рабочих: {working_count}/{len(checked_streams)}")
        return checked_streams


class M3UPlaylist:
    """Генератор M3U плейлистов"""
    
    def __init__(self, output_dir: Path = DATA_DIR):
        self.output_dir = output_dir
    
    def generate_m3u(self, streams: List[StreamInfo], filename: str = "playlist.m3u") -> Path:
        """Сгенерировать M3U файл"""
        output_path = self.output_dir / filename
        
        # Фильтруем только рабочие потоки
        working_streams = [s for s in streams if s.status == "working"]
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
            f.write(f"# Обновлён: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# Всего каналов: {len(working_streams)}\n")
            f.write(f"# LiveM3U - Автоматический генератор плейлистов\n\n")
            
            # Сортируем по категориям
            categories = {}
            for stream in working_streams:
                if stream.category not in categories:
                    categories[stream.category] = []
                categories[stream.category].append(stream)
            
            for category in sorted(categories.keys()):
                f.write(f"\n# {category}\n")
                for stream in categories[category]:
                    f.write(stream.to_m3u_line() + "\n")
        
        logger.info(f"Плейлист сохранён: {output_path} ({len(working_streams)} каналов)")
        return output_path
    
    def generate_m3u8(self, streams: List[StreamInfo], filename: str = "playlist.m3u8") -> Path:
        """Сгенерировать M3U8 файл (для HLS)"""
        return self.generate_m3u(streams, filename)
    
    def save_statistics(self, streams: List[StreamInfo]) -> Path:
        """Сохранить статистику"""
        stats_path = self.output_dir / "statistics.json"
        
        stats = {
            "generated_at": datetime.now().isoformat(),
            "total_streams": len(streams),
            "working_streams": sum(1 for s in streams if s.status == "working"),
            "dead_streams": sum(1 for s in streams if s.status == "dead"),
            "unknown_streams": sum(1 for s in streams if s.status == "unknown"),
            "categories": {}
        }
        
        # Статистика по категориям
        for stream in streams:
            if stream.category not in stats["categories"]:
                stats["categories"][stream.category] = {"total": 0, "working": 0}
            stats["categories"][stream.category]["total"] += 1
            if stream.status == "working":
                stats["categories"][stream.category]["working"] += 1
        
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        
        logger.info(f"Статистика сохранена: {stats_path}")
        return stats_path


class LiveM3UBot:
    """Основной класс бота"""
    
    def __init__(self, check_interval: int = 3600):
        """
        Инициализация бота
        :param check_interval: Интервал проверки в секундах (по умолчанию 1 час)
        """
        self.finder = StreamFinder()
        self.playlist = M3UPlaylist()
        self.check_interval = check_interval
        self.running = False
    
    def run_once(self) -> Tuple[int, int]:
        """Выполнить одну итерацию поиска и обновления"""
        logger.info("=" * 50)
        logger.info("Запуск LiveM3U Bot")
        logger.info("=" * 50)
        
        # Поиск потоков
        streams = self.finder.find_streams()
        
        # Проверка потоков
        checked_streams = self.finder.check_all_streams(streams)
        
        # Генерация плейлиста
        self.playlist.generate_m3u(checked_streams)
        self.playlist.generate_m3u8(checked_streams)
        
        # Сохранение статистики
        self.playlist.save_statistics(checked_streams)
        
        working = sum(1 for s in checked_streams if s.status == "working")
        total = len(checked_streams)
        
        logger.info("=" * 50)
        logger.info(f"Готово! Рабочих каналов: {working}/{total}")
        logger.info("=" * 50)
        
        return working, total
    
    def run_continuous(self):
        """Запустить в непрерывном режиме"""
        self.running = True
        logger.info(f"Запуск в непрерывном режиме (интервал: {self.check_interval}с)")
        
        while self.running:
            try:
                self.run_once()
                logger.info(f"Следующая проверка через {self.check_interval} секунд...")
                time.sleep(self.check_interval)
            except KeyboardInterrupt:
                logger.info("Остановка по запросу пользователя")
                self.stop()
            except Exception as e:
                logger.error(f"Ошибка в основном цикле: {e}")
                time.sleep(60)  # Ждём минуту перед повторной попыткой
    
    def stop(self):
        """Остановить бота"""
        self.running = False
        logger.info("Бот остановлен")


def main():
    """Точка входа"""
    import argparse
    
    parser = argparse.ArgumentParser(description="LiveM3U - Автоматический генератор IPTV плейлистов")
    parser.add_argument("--once", action="store_true", help="Выполнить один раз и выйти")
    parser.add_argument("--interval", type=int, default=3600, help="Интервал проверки в секундах (по умолчанию 3600)")
    parser.add_argument("--workers", type=int, default=10, help="Количество потоков для проверки (по умолчанию 10)")
    
    args = parser.parse_args()
    
    bot = LiveM3UBot(check_interval=args.interval)
    
    if args.once:
        bot.run_once()
    else:
        bot.run_continuous()


if __name__ == "__main__":
    main()
