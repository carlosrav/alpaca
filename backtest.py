import os
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

def main():
    import sys
    compuesto = "--compuesto" in sys.argv
    # Cargar variables de entorno
    load_dotenv()
    
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    
    if not api_key or not secret_key:
        print("Error: No se encontraron las credenciales en el archivo .env")
        return
        
    print("Iniciando simulación (Backtest) de TSLA...")
    print("Rango: 1 de Enero de 2026 al 22 de Junio de 2026")
    
    # 1. Obtener datos históricos de Alpaca
    client = StockHistoricalDataClient(api_key, secret_key)
    
    # Solicitamos barras por Hora para mayor precisión de intradía
    req = StockBarsRequest(
        symbol_or_symbols="MSFT",#["AMD", "TSLA", "NVDA", "AMZN", "MSFT"],#"TSLA",
        timeframe=TimeFrame.Hour,
        start=datetime(2020, 9, 1),
        end=datetime(2026, 6, 28)
    )
    
    try:
        bars = client.get_stock_bars(req)
        df = bars.df
        # Limpiar el índice para facilitar la lectura
        df = df.reset_index()
    except Exception as e:
        print(f"Error al obtener datos históricos: {e}")
        return
        
    print(f"Datos descargados con éxito. Total de horas de trading analizadas: {len(df)}")
    
    # 2. Configuración de parámetros de la estrategia
    starting_cash = 100000.0
    cash = starting_cash
    purchases = [] # Pila LIFO de compras: [{"price": float, "qty": float}]
    
    buy_amount = 10000.0
    max_buys = 10
    buy_drop_pct = 0.05
    sell_rise_pct = 0.08
    
    print(f"Modo compuesto: {'ACTIVO (buy_amount = equity / 10)' if compuesto else 'INACTIVO (buy_amount = $10,000 fijo)'}")
    
    # Estadísticas del backtest
    total_buys_count = 0
    total_sells_count = 0
    trades_log = []
    
    # Crear/Limpiar el archivo backtest.log
    log_file_path = "backtest.log"
    with open(log_file_path, "w", encoding="utf-8") as lf:
        lf.write(f"=== INICIO DE SIMULACIÓN HISTÓRICA TSLA ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===\n")
        lf.write(f"Rango del Backtest: 1 de Enero de 2026 al 22 de Junio de 2026\n")
        lf.write(f"Modo Compuesto: {'Sí (buy_amount = equity / 10)' if compuesto else 'No (buy_amount = $10,000 fijo)'}\n")
        lf.write(f"Capital Inicial: ${starting_cash:,.2f}\n")
        lf.write("=================================================================================\n\n")
    
    # 3. Ejecución de la simulación barra a barra
    for index, row in df.iterrows():
        timestamp = row['timestamp']
        close_price = float(row['close'])
        
        # Caso A: No hay compras activas. Ejecutamos la compra inicial.
        if len(purchases) == 0:
            current_buy_amount = (cash / 10.0) if compuesto else buy_amount
            qty = current_buy_amount / close_price
            cash -= current_buy_amount
            purchase_info = {"price": close_price, "qty": qty, "timestamp": timestamp}
            purchases.append(purchase_info)
            total_buys_count += 1
            
            # Balance total
            total_equity = cash + (qty * close_price)
            
            trades_log.append({
                "type": "BUY_INIT",
                "price": close_price,
                "qty": qty,
                "cash_remaining": cash,
                "timestamp": timestamp
            })
            
            with open(log_file_path, "a", encoding="utf-8") as lf:
                lf.write(f"[{timestamp.strftime('%Y-%m-%d %H:%M')}] COMPRA INICIAL: {qty:.6f} acciones a ${close_price:.2f} (Valor: ${current_buy_amount:,.2f}). Efectivo: ${cash:,.2f} | Balance Total: ${total_equity:,.2f}\n")
            continue
            
        # Obtener última compra de la pila
        last_purchase = purchases[-1]
        last_buy_price = last_purchase["price"]
        
        buy_target = last_buy_price * (1.0 - buy_drop_pct)
        sell_target = last_buy_price * (1.0 + sell_rise_pct)
        
        # Caso B: El precio cruzó el objetivo de compra (-5%)
        if close_price <= buy_target:
            if len(purchases) < max_buys:
                current_equity = cash + sum(p['qty'] for p in purchases) * close_price
                current_buy_amount = (current_equity / 10.0) if compuesto else buy_amount
                if current_buy_amount > cash:
                    current_buy_amount = cash
                
                if current_buy_amount > 0:
                    qty = current_buy_amount / close_price
                    cash -= current_buy_amount
                    purchase_info = {"price": close_price, "qty": qty, "timestamp": timestamp}
                    purchases.append(purchase_info)
                    total_buys_count += 1
                    
                    # Calcular balance total actual
                    holdings_val = sum(p['qty'] for p in purchases) * close_price
                    total_equity = cash + holdings_val
                    
                    trades_log.append({
                        "type": "BUY_GRID",
                        "price": close_price,
                        "qty": qty,
                        "cash_remaining": cash,
                        "timestamp": timestamp
                    })
                    
                    with open(log_file_path, "a", encoding="utf-8") as lf:
                        lf.write(f"[{timestamp.strftime('%Y-%m-%d %H:%M')}] COMPRA GRID: {qty:.6f} acciones a ${close_price:.2f} (Valor: ${current_buy_amount:,.2f}). Efectivo: ${cash:,.2f} | Balance Total: ${total_equity:,.2f} | Compras activas: {len(purchases)}/10\n")
                    
        # Caso C: El precio cruzó el objetivo de venta (+4%)
        elif close_price >= sell_target:
            # Vender el último lote comprado (LIFO)
            qty_to_sell = last_purchase["qty"]
            revenue = qty_to_sell * close_price
            cash += revenue
            removed_purchase = purchases.pop()
            total_sells_count += 1
            
            # Calcular balance total actual
            holdings_val = sum(p['qty'] for p in purchases) * close_price
            total_equity = cash + holdings_val
            original_cost = removed_purchase["qty"] * removed_purchase["price"]
            profit = revenue - original_cost
            
            trades_log.append({
                "type": "SELL",
                "buy_price": removed_purchase["price"],
                "sell_price": close_price,
                "qty": qty_to_sell,
                "profit": profit,
                "cash_remaining": cash,
                "timestamp": timestamp
            })
            
            with open(log_file_path, "a", encoding="utf-8") as lf:
                lf.write(f"[{timestamp.strftime('%Y-%m-%d %H:%M')}] VENTA LOTE: {qty_to_sell:.6f} acciones a ${close_price:.2f} (Compra original a ${removed_purchase['price']:.2f}). Ganancia lote: ${profit:+,.2f} | Efectivo: ${cash:,.2f} | Balance Total: ${total_equity:,.2f} | Compras activas restantes: {len(purchases)}/10\n")
            
    # 4. Resultados finales
    final_price = float(df.iloc[-1]['close'])
    remaining_shares = sum(p['qty'] for p in purchases)
    holdings_value = remaining_shares * final_price
    total_equity = cash + holdings_value
    total_profit = total_equity - starting_cash
    roi = (total_profit / starting_cash) * 100
    
    # Escribir resumen final al archivo .log
    with open(log_file_path, "a", encoding="utf-8") as lf:
        lf.write("\n=================================================================================\n")
        lf.write("                             RESULTADOS FINALES                                  \n")
        lf.write("=================================================================================\n")
        lf.write(f"Período:            {df.iloc[0]['timestamp'].strftime('%Y-%m-%d')} a {df.iloc[-1]['timestamp'].strftime('%Y-%m-%d')}\n")
        lf.write(f"Precio Inicial TSLA: ${df.iloc[0]['close']:.2f}\n")
        lf.write(f"Precio Final TSLA:   ${final_price:.2f}\n")
        lf.write(f"Capital Inicial:     ${starting_cash:,.2f}\n")
        lf.write(f"Efectivo Final:      ${cash:,.2f}\n")
        lf.write(f"Valor de Acciones:   ${holdings_value:,.2f} ({remaining_shares:.6f} acciones)\n")
        lf.write(f"Capital Final Total: ${total_equity:,.2f}\n")
        lf.write(f"Ganancia/Pérdida:    ${total_profit:+,.2f}\n")
        lf.write(f"Retorno (ROI):       {roi:+.2f}%\n")
        lf.write(f"Total de Compras:    {total_buys_count}\n")
        lf.write(f"Total de Ventas:     {total_sells_count}\n")
        lf.write(f"Compras Activas:     {len(purchases)}/10\n")
        lf.write("=================================================================================\n")
        
    print("\n==================================================")
    print("             RESULTADOS DEL BACKTEST              ")
    print("==================================================")
    print(f"Período:            {df.iloc[0]['timestamp'].strftime('%Y-%m-%d')} a {df.iloc[-1]['timestamp'].strftime('%Y-%m-%d')}")
    print(f"Precio Inicial TSLA: ${df.iloc[0]['close']:.2f}")
    print(f"Precio Final TSLA:   ${final_price:.2f}")
    print("--------------------------------------------------")
    print(f"Capital Inicial:     ${starting_cash:,.2f}")
    print(f"Efectivo Final:      ${cash:,.2f}")
    print(f"Valor de Acciones:   ${holdings_value:,.2f} ({remaining_shares:.6f} acciones)")
    print(f"Capital Final Total: ${total_equity:,.2f}")
    print("--------------------------------------------------")
    print(f"Ganancia/Pérdida:    ${total_profit:+,.2f}")
    print(f"Retorno (ROI):       {roi:+.2f}%")
    print("--------------------------------------------------")
    print(f"Total de Compras:    {total_buys_count}")
    print(f"Total de Ventas:     {total_sells_count}")
    print(f"Compras Activas:     {len(purchases)}/10")
    print("==================================================")
    print(f"Detalle completo guardado en: {log_file_path}")
    
    print("\nÚltimas 10 transacciones registradas:")
    for t in trades_log[-10:]:
        date_str = t['timestamp'].strftime('%Y-%m-%d %H:%M')
        if "BUY" in t['type']:
            print(f"[{date_str}] COMPRA - Precio: ${t['price']:.2f} | Acciones: {t['qty']:.4f} | Efectivo: ${t['cash_remaining']:,.2f}")
        else:
            print(f"[{date_str}] VENTA  - Compra: ${t['buy_price']:.2f} -> Venta: ${t['sell_price']:.2f} | Ganancia: ${t['profit']:+,.2f} | Efectivo: ${t['cash_remaining']:,.2f}")

if __name__ == '__main__':
    main()
