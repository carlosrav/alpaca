import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime
from dotenv import load_dotenv

# Importaciones de Alpaca SDK
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# Parsear argumentos de consola para el activo
parser = argparse.ArgumentParser(description="Bot de Grid Trading Genérico para Alpaca")
parser.add_argument("--activo", type=str, required=True, help="Símbolo del activo a operar (ej. AMZN, AAPL, TSLA)")
args, unknown = parser.parse_known_args()

SYMBOL = args.activo.upper()
STATE_FILE = f"{SYMBOL}_state.json"
LOG_FILE = f"{SYMBOL}_bot.log"

# Configuración dinámica de logs según el activo
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

BUY_AMOUNT = 10000.0  # Monto en USD por compra
MAX_BUYS = 10         # Máximo de compras en la cuadrícula
BUY_DROP_PCT = 0.05   # 5% de caída
SELL_RISE_PCT = 0.04  # 4% de subida
CHECK_INTERVAL_SEC = 1200  # Intervalo de monitoreo (20 minutos)

def load_state():
    """Carga el estado del bot desde el archivo JSON local."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                # Asegurar la estructura básica
                if "purchases" not in state:
                    state["purchases"] = []
                return state
        except Exception as e:
            logging.error(f"Error al leer el archivo de estado {STATE_FILE}: {e}. Se iniciará un estado vacío.")
    return {"purchases": []}

def save_state(state):
    """Guarda el estado del bot en el archivo JSON local."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
        logging.debug("Estado guardado correctamente.")
    except Exception as e:
        logging.error(f"Error al escribir en el archivo de estado {STATE_FILE}: {e}")

def sync_state_with_server(trading_client, symbol):
    """Sincroniza el estado del JSON local con las operaciones (posición) abiertas en Alpaca."""
    logging.info(f"Sincronizando estado local con el servidor Alpaca para {symbol}...")
    try:
        # 1. Obtener la posición abierta actual en Alpaca
        try:
            position = trading_client.get_open_position(symbol)
            position_qty = float(position.qty)
            avg_entry_price = float(position.avg_entry_price)
            logging.info(f"Posición abierta encontrada en Alpaca: {position_qty:.6f} acciones de {symbol} a un precio promedio de ${avg_entry_price:.2f}")
        except Exception as e:
            # Si no hay posición abierta (404 Position not found)
            if "not found" in str(e).lower() or "404" in str(e):
                logging.info(f"No se encontró posición abierta para {symbol} en Alpaca. Limpiando compras en el JSON.")
                state = {"purchases": []}
                save_state(state)
                return state
            else:
                logging.error(f"Error al obtener la posición de {symbol} en Alpaca: {e}")
                # En caso de error de conexión/API, devolvemos el estado local existente
                return load_state()

        # 2. Obtener órdenes cerradas para reconstruir los lotes usando lógica LIFO
        req_params = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            symbols=[symbol],
            limit=100
        )
        orders = trading_client.get_orders(filter=req_params)
        
        # Filtrar solo las que estén FILLED y tengan fecha de ejecución
        filled_orders = [o for o in orders if o.status == OrderStatus.FILLED and o.filled_at is not None]
        # Ordenar por filled_at ascendente (más antiguas primero)
        filled_orders.sort(key=lambda o: o.filled_at)
        
        reconstructed_purchases = []
        for order in filled_orders:
            qty = float(order.filled_qty)
            price = float(order.filled_avg_price)
            order_id = str(order.id)
            timestamp = order.filled_at.isoformat()
            
            if order.side == OrderSide.BUY:
                reconstructed_purchases.append({
                    "price": price,
                    "qty": qty,
                    "order_id": order_id,
                    "timestamp": timestamp
                })
            elif order.side == OrderSide.SELL:
                # Lógica LIFO: descontar de las compras más recientes
                sell_qty = qty
                while sell_qty > 0 and reconstructed_purchases:
                    last_buy = reconstructed_purchases[-1]
                    if last_buy["qty"] <= sell_qty:
                        sell_qty -= last_buy["qty"]
                        reconstructed_purchases.pop()
                    else:
                        last_buy["qty"] -= sell_qty
                        sell_qty = 0

        # 3. Ajustar el total reconstruido para que coincida exactamente con la posición real
        recon_total = sum(p["qty"] for p in reconstructed_purchases)
        tolerance = 1e-5
        if abs(recon_total - position_qty) > tolerance:
            logging.warning(f"La cantidad reconstruida ({recon_total:.6f}) difiere de la posición real en Alpaca ({position_qty:.6f}). Ajustando...")
            if position_qty == 0:
                reconstructed_purchases = []
            elif recon_total < position_qty:
                # Añadir la diferencia como un lote de respaldo (histórico)
                diff = position_qty - recon_total
                reconstructed_purchases.insert(0, {
                    "price": avg_entry_price,
                    "qty": diff,
                    "order_id": "historical-fallback",
                    "timestamp": datetime.utcnow().isoformat()
                })
            else:
                # Recortar desde las más antiguas (al principio de la lista) para mantener las más recientes
                adjusted_purchases = []
                remaining_to_keep = position_qty
                for p in reversed(reconstructed_purchases):
                    if remaining_to_keep <= 0:
                        break
                    if p["qty"] <= remaining_to_keep:
                        adjusted_purchases.append(p)
                        remaining_to_keep -= p["qty"]
                    else:
                        p["qty"] = remaining_to_keep
                        adjusted_purchases.append(p)
                        remaining_to_keep = 0
                adjusted_purchases.reverse()
                reconstructed_purchases = adjusted_purchases

        # Guardar en el archivo de estado
        state = {"purchases": reconstructed_purchases}
        save_state(state)
        logging.info(f"Sincronización completada. {len(reconstructed_purchases)} lotes cargados en {STATE_FILE}.")
        return state

    except Exception as e:
        logging.error(f"Error durante la sincronización de estado con el servidor: {e}")
        return load_state()

def verify_and_sync_after_operation(trading_client, symbol, order_id):
    """
    Verifica que la orden esté realmente en estado FILLED en el servidor
    antes de actualizar el archivo JSON mediante sincronización.
    """
    logging.info(f"Cerciorándose del estado de la orden {order_id} en el servidor...")
    try:
        order = trading_client.get_order_by_id(order_id)
        if order.status == OrderStatus.FILLED:
            logging.info(f"La orden {order_id} está confirmada como FILLED. Procediendo a sincronizar con el servidor.")
            # Esperar un breve instante para dar tiempo a que Alpaca actualice la posición en su API
            time.sleep(1)
            sync_state_with_server(trading_client, symbol)
            return True
        else:
            logging.warning(f"La orden {order_id} se encuentra en estado {order.status} (no FILLED). No se actualizará el JSON local.")
            return False
    except Exception as e:
        logging.error(f"Error al verificar la orden {order_id} en el servidor: {e}")
        return False

def get_latest_price(data_client, symbol):
    """Obtiene el último precio de negociación (Last Trade) para el símbolo."""
    try:
        request_params = StockLatestTradeRequest(symbol_or_symbols=symbol)
        latest_trade = data_client.get_stock_latest_trade(request_params)
        return float(latest_trade[symbol].price)
    except Exception as e:
        logging.error(f"Error al obtener el precio más reciente de {symbol}: {e}")
        return None

def wait_for_order_fill(trading_client, order_id, max_attempts=15, delay=1):
    """Espera a que una orden se llene completamente (FILLED)."""
    for attempt in range(max_attempts):
        try:
            order = trading_client.get_order_by_id(order_id)
            if order.status == OrderStatus.FILLED:
                return order
            elif order.status in [OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED]:
                raise Exception(f"La orden fue {order.status.value}")
        except Exception as e:
            if "La orden fue" in str(e):
                raise e
            logging.warning(f"Intento {attempt + 1}: Error al consultar la orden {order_id}: {e}")
        time.sleep(delay)
    raise TimeoutError(f"La orden {order_id} no se completó en el tiempo esperado.")

def execute_buy(trading_client, symbol, amount):
    """Ejecuta una compra a mercado de una cantidad en USD (Notional)."""
    logging.info(f"Enviando orden de COMPRA a mercado para {symbol} por ${amount} USD...")
    try:
        req = MarketOrderRequest(
            symbol=symbol,
            notional=amount,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        order = trading_client.submit_order(req)
        logging.info(f"Orden de compra enviada. ID: {order.id}. Esperando ejecución...")
        
        filled_order = wait_for_order_fill(trading_client, order.id)
        
        # Extraer detalles
        filled_price = float(filled_order.filled_avg_price)
        filled_qty = float(filled_order.filled_qty)
        filled_at = filled_order.filled_at.isoformat() if filled_order.filled_at else datetime.utcnow().isoformat()
        
        logging.info(f"¡COMPRA COMPLETADA! Precio Promedio: ${filled_price:.2f}, Acciones: {filled_qty:.6f}")
        return {
            "price": filled_price,
            "qty": filled_qty,
            "order_id": str(filled_order.id),
            "timestamp": filled_at
        }
    except Exception as e:
        logging.error(f"Error al ejecutar la compra: {e}")
        return None

def execute_sell(trading_client, symbol, qty):
    """Ejecuta una venta a mercado de una cantidad específica de acciones (qty)."""
    logging.info(f"Enviando orden de VENTA a mercado para {symbol} de {qty:.6f} acciones...")
    try:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        )
        order = trading_client.submit_order(req)
        logging.info(f"Orden de venta enviada. ID: {order.id}. Esperando ejecución...")
        
        filled_order = wait_for_order_fill(trading_client, order.id)
        
        filled_price = float(filled_order.filled_avg_price)
        filled_qty = float(filled_order.filled_qty)
        filled_at = filled_order.filled_at.isoformat() if filled_order.filled_at else datetime.utcnow().isoformat()
        
        logging.info(f"¡VENTA COMPLETADA! Precio Promedio: ${filled_price:.2f}, Acciones: {filled_qty:.6f}")
        return {
            "price": filled_price,
            "qty": filled_qty,
            "order_id": str(filled_order.id),
            "timestamp": filled_at
        }
    except Exception as e:
        logging.error(f"Error al ejecutar la venta: {e}")
        return None

def main():
    # Cargar .env
    load_dotenv()
    
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    
    if not api_key or not secret_key:
        logging.error("Credenciales de Alpaca faltantes en el archivo .env. Finalizando.")
        return
        
    logging.info(f"Iniciando Bot de Grid Trading para {SYMBOL}...")
    
    # Inicializar clientes
    trading_client = TradingClient(api_key, secret_key, paper=True)
    data_client = StockHistoricalDataClient(api_key, secret_key)
    
    # Verificar conexión/cuenta
    try:
        account = trading_client.get_account()
        logging.info(f"Conexión exitosa a Alpaca. Cuenta Demo: #{account.account_number}")
        logging.info(f"Efectivo Disponible: ${float(account.cash):,.2f} | Valor de Portafolio: ${float(account.portfolio_value):,.2f}")
    except Exception as e:
        logging.error(f"Error al conectar con Alpaca: {e}")
        return

    # Cargar estado y sincronizar con el servidor al iniciar
    state = sync_state_with_server(trading_client, SYMBOL)
    purchases = state["purchases"]
    
    logging.info(f"Estado inicial cargado. Compras activas en la cuadrícula: {len(purchases)}")
    for i, p in enumerate(purchases):
        logging.info(f"  [{i+1}] Compra a ${p['price']:.2f} | Cantidad: {p['qty']:.6f} acciones | Fecha: {p['timestamp']}")

    # Bucle de control
    while True:
        try:
            # Cargar estado y lista de compras en cada iteración para reflejar cambios externos en el JSON dinámico
            state = load_state()
            purchases = state["purchases"]
            
            # Verificar si el mercado está abierto para operar
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    logging.info(f"El mercado está cerrado. Próxima apertura: {clock.next_open}. Esperando al próximo ciclo...")
                    time.sleep(CHECK_INTERVAL_SEC)
                    continue
            except Exception as clock_err:
                logging.error(f"Error al verificar el estado del mercado: {clock_err}. Continuando...")
            
            current_price = get_latest_price(data_client, SYMBOL)
            if current_price is None:
                logging.warning("No se pudo obtener el precio actual. Reintentando en el próximo ciclo...")
                time.sleep(CHECK_INTERVAL_SEC)
                continue
                
            logging.info(f"Precio actual de {SYMBOL}: ${current_price:.2f}")
            
            # Caso 1: No hay compras iniciales. Ejecutamos la primera compra de $10,000 para iniciar.
            if len(purchases) == 0:
                logging.info("No hay compras registradas en el estado. Ejecutando compra inicial...")
                buy_info = execute_buy(trading_client, SYMBOL, BUY_AMOUNT)
                if buy_info:
                    if verify_and_sync_after_operation(trading_client, SYMBOL, buy_info["order_id"]):
                        logging.info("Grid de trading iniciado y verificado con el servidor.")
                    else:
                        logging.warning("No se pudo verificar la compra en el servidor. El JSON local no fue actualizado.")
                time.sleep(CHECK_INTERVAL_SEC)
                continue
            
            # Obtener datos de la última compra
            last_purchase = purchases[-1]
            last_buy_price = last_purchase["price"]
            
            # Calcular límites de precio
            buy_target = last_buy_price * (1.0 - BUY_DROP_PCT)
            sell_target = last_buy_price * (1.0 + SELL_RISE_PCT)
            
            logging.info(f"-> Última compra: ${last_buy_price:.2f} | Compras actuales: {len(purchases)}/{MAX_BUYS}")
            logging.info(f"-> Objetivo de COMPRA (caída a): ${buy_target:.2f} | Objetivo de VENTA (subida a): ${sell_target:.2f}")
            
            # Caso 2: El precio ha caído un 5% o más respecto a la última compra
            if current_price <= buy_target:
                if len(purchases) < MAX_BUYS:
                    logging.info(f"¡Condición de compra detectada! Precio actual ${current_price:.2f} <= Objetivo ${buy_target:.2f}")
                    buy_info = execute_buy(trading_client, SYMBOL, BUY_AMOUNT)
                    if buy_info:
                        if verify_and_sync_after_operation(trading_client, SYMBOL, buy_info["order_id"]):
                            logging.info("Nueva compra verificado con el servidor y registrada.")
                        else:
                            logging.warning("No se pudo verificar la compra en el servidor. El JSON local no fue actualizado.")
                else:
                    logging.warning(f"El precio cayó a ${current_price:.2f}, pero ya se alcanzó el límite máximo de {MAX_BUYS} compras.")
            
            # Caso 3: El precio ha subido un 4% o más respecto a la última compra
            elif current_price >= sell_target:
                logging.info(f"¡Condición de venta detectada! Precio actual ${current_price:.2f} >= Objetivo ${sell_target:.2f}")
                sell_info = execute_sell(trading_client, SYMBOL, last_purchase["qty"])
                if sell_info:
                    if verify_and_sync_after_operation(trading_client, SYMBOL, sell_info["order_id"]):
                        logging.info("Venta de lote verificado con el servidor. Estado actualizado.")
                    else:
                        logging.warning("No se pudo verificar la venta en el servidor. El JSON local no fue actualizado.")
            
            else:
                logging.info("El precio se mantiene dentro del rango. No se requieren acciones.")
                
        except Exception as e:
            logging.error(f"Error inesperado en el bucle principal: {e}")
            
        time.sleep(CHECK_INTERVAL_SEC)

if __name__ == '__main__':
    main()
