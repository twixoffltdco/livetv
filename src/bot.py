"""
LiveM3U - Собственный поисковой робот IPTV каналов РФ
Ищет рабочие потоки САМОСТОЯТЕЛЬНО, без использования готовых списков (iptv-org и др.)
Сканирует IP диапазоны, проверяет популярные паттерны URL, находит рабочие потоки
"""

import asyncio
import aiohttp
import re
import json
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Set
import ipaddress

# Конфигурация
DATA_DIR = Path("/workspace/data")
CONFIG_DIR = Path("/workspace/config")
PLAYLIST_FILE = DATA_DIR / "playlist.m3u"
FOUND_STREAMS_FILE = DATA_DIR / "found_streams.json"
LOG_FILE = DATA_DIR / "search.log"

# Популярные порты для IPTV
IPTV_PORTS = [80, 443, 8080, 8000, 8008, 8888, 9000, 1935]

# Ключевые слова для поиска российских каналов
RU_KEYWORDS = [
    'russia', 'ru_', '_ru', 'moscow', 'spb', 'piter',
    'первый', 'россия', 'нтв', 'тнт', 'стс', 'рен', '5кан',
    'матч', 'звезда', 'мир', 'дождь', 'rtvi',
    'news', 'sport', 'kino', 'film', 'deti', 'music'
]

class IPTVScanner:
    """Поисковой робот для самостоятельного нахождения IPTV потоков"""
    
    def __init__(self):
        self.found_streams: Dict[str, Dict] = {}
        self.scanned_ips: Set[str] = set()
        self.session = None
        self.semaphore = asyncio.Semaphore(50)
        
    async def init_session(self):
        timeout = aiohttp.ClientTimeout(total=5)
        self.session = aiohttp.ClientSession(timeout=timeout)
        
    async def close_session(self):
        if self.session:
            await self.session.close()
    
    def log(self, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{timestamp}] {message}"
        print(log_msg)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_msg + '\n')
    
    async def check_and_add(self, url: str) -> bool:
        """Проверка URL и добавление если это рабочий IPTV поток"""
        async with self.semaphore:
            try:
                async with self.session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=3)) as response:
                    content_type = response.headers.get('Content-Type', '')
                    text = await response.text()
                    
                    is_valid = False
                    channel_name = "Unknown"
                    
                    # Проверка на валидный IPTV поток
                    if any(x in content_type for x in ['application/vnd.apple.mpegurl', 'application/dash+xml', 'audio/x-mpegurl']):
                        is_valid = True
                    elif text.startswith('#EXTM3U'):
                        is_valid = True
                        match = re.search(r'#EXTINF:-1.*?,\s*(.+)', text)
                        if match:
                            channel_name = match.group(1).strip()
                    elif '#EXTINF:' in text[:500]:
                        is_valid = True
                        match = re.search(r'#EXTINF:-1.*?,\s*(.+)', text[:500])
                        if match:
                            channel_name = match.group(1).strip()
                    elif '<MPD' in text[:500]:
                        is_valid = True
                    
                    if is_valid:
                        # Проверяем на российскую принадлежность
                        is_ru = any(kw in url.lower() for kw in RU_KEYWORDS) or \
                                any(kw in channel_name.lower() for kw in RU_KEYWORDS)
                        
                        # Добавляем если русский ИЛИ пока мало найдено
                        if is_ru or len(self.found_streams) < 100:
                            self.found_streams[url] = {
                                'name': channel_name,
                                'url': url,
                                'found_at': datetime.now().isoformat(),
                                'country': 'RU' if is_ru else 'UNKNOWN'
                            }
                            return True
                    return False
            except Exception:
                return False
    
    async def scan_url_pattern(self, base_url: str, patterns: List[str]):
        """Сканирование URL по паттернам"""
        tasks = []
        for pattern in patterns:
            url = f"{base_url}{pattern}"
            if url not in self.found_streams:
                tasks.append(self.check_and_add(url))
        
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for url, result in zip([f"{base_url}{p}" for p in patterns], results):
                if isinstance(result, bool) and result:
                    self.log(f"✅ Найден поток: {url}")
    
    async def scan_ip_range(self, ip_range: str):
        """Сканирование диапазона IP адресов на наличие IPTV потоков"""
        try:
            network = ipaddress.ip_network(ip_range, strict=False)
            tasks = []
            
            for ip in network:
                if ip in self.scanned_ips:
                    continue
                self.scanned_ips.add(str(ip))
                
                for port in IPTV_PORTS[:3]:  # Только основные порты
                    base_url = f"http://{ip}:{port}" if port != 80 else f"http://{ip}"
                    patterns = ['/live.m3u8', '/stream.m3u8', '/iptv.m3u8', '/hls/stream.m3u8', 
                               '/live/index.m3u8', '/channel.m3u8', '/tv.m3u8']
                    tasks.append(self.scan_url_pattern(base_url, patterns))
            
            # Выполняем батчами по 20
            for i in range(0, len(tasks), 20):
                batch = tasks[i:i+20]
                await asyncio.gather(*batch, return_exceptions=True)
                self.log(f"Просканировано {min(i+20, len(tasks))} из {len(tasks)} адресов")
                
        except Exception as e:
            self.log(f"Ошибка сканирования диапазона {ip_range}: {e}")
    
    async def search_providers(self):
        """Поиск через известные паттерны российских провайдеров"""
        self.log("🔍 Поиск через анализ популярных IPTV паттернов провайдеров РФ...")
        
        # Популярные домены провайдеров и ТВ сервисов РФ
        ru_providers = [
            'ertelecom.ru', 'rostelecom.ru', 'domru.ru', 'byfly.by', 
            'megafon.ru', 'beeline.ru', 'mts.ru', 'tvzavr.ru', 
            'peers.tv', 'forkplayer.com', 'numb.ru', 'iptv-plus.ru',
            '24tv.ru', 'smotrim.ru', '1tv.ru', 'vgtrk.com'
        ]
        
        tasks = []
        for provider in ru_providers:
            for ext in ['.m3u8', '.mpd', '.m3u']:
                # Основные паттерны
                patterns_to_try = [
                    f"https://{provider}/stream{ext}",
                    f"https://{provider}/live{ext}",
                    f"https://{provider}/iptv{ext}",
                    f"https://{provider}/hls/stream{ext}",
                    f"https://{provider}/live/index{ext}",
                    f"https://{provider}/channel{ext}",
                    f"https://{provider}/tv{ext}",
                ]
                for url in patterns_to_try:
                    tasks.append(self.check_and_add(url))
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def load_existing_streams(self):
        """Загрузка ранее найденных потоков"""
        if FOUND_STREAMS_FILE.exists():
            try:
                with open(FOUND_STREAMS_FILE, 'r', encoding='utf-8') as f:
                    self.found_streams = json.load(f)
                self.log(f"📂 Загружено {len(self.found_streams)} ранее найденных потоков")
            except Exception:
                pass
    
    async def save_streams(self):
        """Сохранение найденных потоков"""
        with open(FOUND_STREAMS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.found_streams, f, ensure_ascii=False, indent=2)
        self.log(f"💾 Сохранено {len(self.found_streams)} потоков")
    
    def generate_m3u(self) -> str:
        """Генерация M3U плейлиста"""
        m3u_content = "#EXTM3U\n"
        m3u_content += f"# Обновлён: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        m3u_content += f"# Всего каналов: {len(self.found_streams)}\n"
        m3u_content += "# Сгенерировано собственным поисковым роботом LiveM3U\n"
        m3u_content += "# НЕ использует iptv-org или другие готовые списки\n\n"
        
        for url, info in self.found_streams.items():
            name = info.get('name', 'Channel')
            group = info.get('country', 'UNKNOWN')
            m3u_content += f'#EXTINF:-1 tvg-name="{name}" group-title="{group}",{name}\n'
            m3u_content += f'{url}\n\n'
        
        return m3u_content
    
    async def run_scan(self, ip_ranges: List[str] = None):
        """Запуск полного сканирования"""
        await self.init_session()
        
        self.log("🚀 Запуск ПОИСКОВОГО РОБОТА LiveM3U...")
        self.log("⚠️ НЕ используем iptv-org или другие готовые списки!")
        await self.load_existing_streams()
        
        # 1. Поиск по паттернам провайдеров
        self.log("📡 Сканирование паттернов российских провайдеров...")
        await self.search_providers()
        
        # 2. Сканирование IP диапазонов (если указаны)
        if ip_ranges:
            self.log(f"🌐 Сканирование {len(ip_ranges)} IP диапазонов...")
            for ip_range in ip_ranges:
                await self.scan_ip_range(ip_range)
        else:
            self.log("ℹ️ IP диапазоны не указаны. Используйте --ip-ranges для сканирования.")
        
        # Итоги
        self.log(f"✅ Найдено потоков: {len(self.found_streams)}")
        
        # Сохранение результатов
        await self.save_streams()
        
        # Генерация M3U
        m3u_content = self.generate_m3u()
        with open(PLAYLIST_FILE, 'w', encoding='utf-8') as f:
            f.write(m3u_content)
        
        self.log(f"📺 Плейлист сохранён: {PLAYLIST_FILE}")
        self.log(f"🎉 Сканирование завершено! Рабочих каналов: {len(self.found_streams)}")
        
        await self.close_session()


async def main():
    parser = argparse.ArgumentParser(description='LiveM3U - Поисковой робот IPTV каналов')
    parser.add_argument('--once', action='store_true', help='Однократный запуск')
    parser.add_argument('--interval', type=int, default=3600, help='Интервал повторного запуска (сек)')
    parser.add_argument('--ip-ranges', nargs='+', help='IP диапазоны для сканирования (например: 195.161.0.0/16)')
    args = parser.parse_args()
    
    # Создание директорий
    DATA_DIR.mkdir(exist_ok=True)
    CONFIG_DIR.mkdir(exist_ok=True)
    
    scanner = IPTVScanner()
    
    if args.once:
        await scanner.run_scan(args.ip_ranges)
    else:
        while True:
            await scanner.run_scan(args.ip_ranges)
            next_run = datetime.now().strftime("%H:%M:%S")
            scanner.log(f"😴 Сон на {args.interval} секунд. Следующий запуск после {next_run}")
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
