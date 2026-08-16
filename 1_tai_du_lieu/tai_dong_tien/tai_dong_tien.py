"""
[AI_CONTEXT]
- MODULE: 1_tai_du_lieu / tai_dong_tien
- ROLE: WebSocket: Hung goi aggTrade tu Binance Spot va Futures.
- I/O: IN: Binance Spot WS + Futures WS | OUT: RAM (bo_nho_ram)
- UPDATE: Tach ro Spot vs Futures stream cho Tri-Oracle Divergence.
"""

import asyncio
import orjson
import websockets
import logging
import time


async def hung_dong_tien_spot(symbol: str, bo_nho_ram):
    """
    [TIER A] AggTrade Binance SPOT - tien mat that, khong don bay.
    Ghi vao: bo_nho_ram.danh_sach_khop_lenh
    """
    url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@aggTrade"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                logging.info(f"[SPOT FLOW] Ket noi aggTrade Spot: {symbol.upper()}")
                async for raw in ws:
                    try:
                        data = orjson.loads(raw)
                        lenh_khop = {
                            'gia': float(data['p']),
                            'khoi_luong': float(data['q']),
                            'ban_chu_dong': bool(data['m']),
                            'thoi_gian_ms': int(data.get('E', time.time() * 1000)),
                            'nguon': 'SPOT'
                        }
                        bo_nho_ram.danh_sach_khop_lenh.append(lenh_khop)
                        bo_nho_ram.thoi_gian_dong_tien_cuoi = time.time()
                    except (KeyError, TypeError, ValueError):
                        continue
        except websockets.exceptions.ConnectionClosed as e:
            logging.warning(f"[SPOT FLOW] Mat ket noi: {e}. Ket noi lai...")
            await asyncio.sleep(3)
        except Exception as e:
            logging.error(f"[SPOT FLOW] Loi: {e}. Thu lai sau 3s...")
            await asyncio.sleep(3)


# Alias tuong thich nguoc — khoi_dong.py dang goi ten cu nay
hung_dong_tien_futures = hung_dong_tien_spot


async def hung_dong_tien_futures_real(symbol: str, bo_nho_ram):
    """
    [TIER C] AggTrade Binance FUTURES - don bay Retail, de bi thao tung.
    Chi dung de so sanh phan ky Tri-Oracle, KHONG dung de ra lenh.
    Ghi vao: bo_nho_ram.danh_sach_khop_lenh_futures
    """
    url = f"wss://fstream.binance.com/ws/{symbol.lower()}@aggTrade"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                logging.info(f"[FUTURES FLOW] Ket noi Futures: {symbol.upper()} (Chi Tri-Oracle)")
                async for raw in ws:
                    try:
                        data = orjson.loads(raw)
                        lenh_khop = {
                            'gia': float(data['p']),
                            'khoi_luong': float(data['q']),
                            'ban_chu_dong': bool(data['m']),
                            'thoi_gian_ms': int(data.get('E', time.time() * 1000)),
                            'nguon': 'FUTURES'
                        }
                        bo_nho_ram.danh_sach_khop_lenh_futures.append(lenh_khop)
                        bo_nho_ram.thoi_gian_dong_tien_futures_cuoi = time.time()
                    except (KeyError, TypeError, ValueError):
                        continue
        except websockets.exceptions.ConnectionClosed as e:
            logging.warning(f"[FUTURES FLOW] Mat ket noi: {e}. Ket noi lai...")
            await asyncio.sleep(3)
        except Exception as e:
            logging.error(f"[FUTURES FLOW] Loi: {e}. Thu lai sau 3s...")
            await asyncio.sleep(3)
