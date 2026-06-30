import os
import json
import time
import logging
from datetime import datetime
from dotenv import load_dotenv

# Importaciones de Alpaca SDK
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# Configuración de logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("tesla_bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

STATE_FILE = "tesla_state.json"
SYMBOL = "NVDA"
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
        
    logging.info("Iniciando Bot de Grid Trading para TSLA...")
    
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

    # Cargar estado
    state = load_state()
    purchases = state["purchases"]
    
    logging.info(f"Estado inicial cargado. Compras activas en la cuadrícula: {len(purchases)}")
    for i, p in enumerate(purchases):
        logging.info(f"  [{i+1}] Compra a ${p['price']:.2f} | Cantidad: {p['qty']:.6f} acciones | Fecha: {p['timestamp']}")

    # Bucle de control
    while True:
        try:
            # Cargar estado y lista de compras en cada iteración para reflejar cambios externos en tesla_state.json
            state = load_state()
            purchases = state["purchases"]
            
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
                    purchases.append(buy_info)
                    save_state(state)
                    logging.info(f"Grid de trading iniciado con compra a ${buy_info['price']:.2f}")
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
                        purchases.append(buy_info)
                        save_state(state)
                        logging.info(f"Nueva compra registrada a ${buy_info['price']:.2f}. Total compras: {len(purchases)}")
                else:
                    logging.warning(f"El precio cayó a ${current_price:.2f}, pero ya se alcanzó el límite máximo de {MAX_BUYS} compras.")
            
            # Caso 3: El precio ha subido un 4% o más respecto a la última compra
            elif current_price >= sell_target:
                logging.info(f"¡Condición de venta detectada! Precio actual ${current_price:.2f} >= Objetivo ${sell_target:.2f}")
                sell_info = execute_sell(trading_client, SYMBOL, last_purchase["qty"])
                if sell_info:
                    # Remover el último bloque comprado de la pila
                    removed_purchase = purchases.pop()
                    save_state(state)
                    
                    logging.info(f"Venta completada del lote comprado a ${removed_purchase['price']:.2f}")
                    if len(purchases) > 0:
                        logging.info(f"La memoria regresa al precio de compra anterior: ${purchases[-1]['price']:.2f}")
                    else:
                        logging.info("Se han vendido todos los lotes. El grid se reiniciará en el próximo ciclo.")
            
            else:
                logging.info("El precio se mantiene dentro del rango. No se requieren acciones.")
                
        except Exception as e:
            logging.error(f"Error inesperado en el bucle principal: {e}")
            
        time.sleep(CHECK_INTERVAL_SEC)

if __name__ == '__main__':
    main()
