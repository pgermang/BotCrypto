
# ==== CONFIGURACIÓN DE ENVÍO DE ALERTAS ====
import os
import sys
import time
import pandas as pd
import aiohttp
import threading
import json
import random
import traceback
import asyncio
import warnings
warnings.filterwarnings("ignore", message="python-telegram-bot is using upstream urllib3")

from telegram.ext import Updater, CommandHandler, CallbackContext
from ta.momentum import RSIIndicator
from ta.trend import MACD
from ta.volatility import AverageTrueRange
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from telegram import Bot, Update
from pathlib import Path

CONFIG_PATH = Path("config.json")
symbols_error_count = {}
SYMBOLS_PATH = Path("symbols.json")
INTERVALS_ENABLED = {}
MIN_VOLUME_BY_INTERVAL = {}
VOLUMEN_EXTREMO_BY_INTERVAL = {}
last_alert_times = {}
last_change_values = {}  # Nuevo diccionario para control de re-alertas
INTERVAL_CYCLE_SECONDS = {}
PATRONES_ACTIVOS = {}
funding_cache = {}
calma_symbols = {}  # {symbol: (sin_alertas, ciclos_restantes)}


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

bot = None
ALERTA_FORMATO_COMPACTO = False
INCLUIR_LINK_BINANCE = True
CHANGE_THRESHOLD_BTC_ETH = {
    "5m": 1.0,
    "1h": 1.5,
    "4h": 0.0,
    "1d": 0.0,
    "1w": 0.0
}

CHANGE_THRESHOLD_DEFAULT = {
    "5m": 2.0,
    "1h": 3.0,
    "4h": 0.0,
    "1d": 0.0,
    "1w": 0.0
}
MIN_ALERT_INTERVAL_MINUTES = 1
SOLO_VELA_CERRADA = False
DELTA_REPETICION_MINIMA = 0.4
MAX_CONCURRENT_TASKS = 10
ACTUALIZAR_SIMBOLOS_CADA_HORAS = 24
RSI_SOBRECOMPRA = 75
RSI_SOBREVENTA = 25
ACTIVAR_LONGS = True
ACTIVAR_SHORTS = True
FUNDING_CACHE_SECONDS = 180  # Duración del cache de funding en segundos
AGRUPAR_ALERTAS = False
DELAY_ALERTAS_MS = 150  # tiempo entre mensajes individuales, en milisegundos
CALMA_CICLOS_MAX = 3  # ciclos de calma tras no cumplir condiciones
api_call_count = 0
api_klines_count = 0
api_funding_count = 0

ESTRATEGIA_DIVERGENCIA_ACTIVA = True
ESTRATEGIA_MOVIMIENTO_ACTIVA = True
INTERVALOS_ENTRADA = ["4h", "1d", "1w"]
INTERVALOS_MOVIMIENTO = ["5m", "15m", "1h", "4h"]
MIN_DIVERGENCE_PRICE_DIFF = 0.5  # % mínimo entre pivots

ADMINS = {
    1294216517,  # TU USER ID
}

#----------------------------------------------------------------------------

def es_admin(update):
    user = update.effective_user
    return user and user.id in ADMINS


async def cargar_o_actualizar_symbols():
    if SYMBOLS_PATH.exists():
        with SYMBOLS_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)

    async with aiohttp.ClientSession() as session:
        symbols = await get_symbols(session)

    with SYMBOLS_PATH.open("w", encoding="utf-8") as f:
        json.dump(symbols, f, indent=2)

    return symbols

def registrar_error_symbol(symbol):
    symbols_error_count[symbol] = symbols_error_count.get(symbol, 0) + 1

    if symbols_error_count[symbol] >= 3:
        try:
            if SYMBOLS_PATH.exists():
                with SYMBOLS_PATH.open("r", encoding="utf-8") as f:
                    symbols = json.load(f)

                if symbol in symbols:
                    symbols.remove(symbol)

                    with SYMBOLS_PATH.open("w", encoding="utf-8") as f:
                        json.dump(symbols, f, indent=2)

                    print(f"[symbols.json] ❌ {symbol} removido tras múltiples errores.")

        except Exception as e:
            print(f"[symbols.json] Error al eliminar símbolo {symbol}: {e}")


# ==== CONTROL DE CONCURRENCIA CON SEMÁFORO ====

semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

async def limitado(func, *args, **kwargs):
    global api_call_count
    async with semaphore:
        api_call_count += 1
        return await func(*args, **kwargs)


def cargar_config():
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, bot
    global ALERTA_FORMATO_COMPACTO, INCLUIR_LINK_BINANCE
    global INTERVALS_ENABLED, CHANGE_THRESHOLD_BTC_ETH, CHANGE_THRESHOLD_DEFAULT, MIN_ALERT_INTERVAL_MINUTES
    global MIN_VOLUME_BY_INTERVAL, INTERVAL_CYCLE_SECONDS, SOLO_VELA_CERRADA, RSI_SOBRECOMPRA, RSI_SOBREVENTA
    global DELTA_REPETICION_MINIMA, ACTUALIZAR_SIMBOLOS_CADA_HORAS, FUNDING_CACHE_SECONDS, DELAY_ALERTAS_MS
    global VOLUMEN_EXTREMO_BY_INTERVAL, PATRONES_ACTIVOS
    global CALMA_CICLOS_MAX
    global ACTIVAR_LONGS, ACTIVAR_SHORTS


    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)

            if 'TELEGRAM_TOKEN' in data:
                TELEGRAM_TOKEN = data['TELEGRAM_TOKEN']
            if 'TELEGRAM_CHAT_ID' in data:
                TELEGRAM_CHAT_ID = data['TELEGRAM_CHAT_ID']

            if TELEGRAM_TOKEN:
                bot = Bot(token=TELEGRAM_TOKEN)

            ALERTA_FORMATO_COMPACTO = data.get("ALERTA_FORMATO_COMPACTO", ALERTA_FORMATO_COMPACTO)
            SOLO_VELA_CERRADA = data.get("SOLO_VELA_CERRADA", SOLO_VELA_CERRADA)
            INCLUIR_LINK_BINANCE = data.get("INCLUIR_LINK_BINANCE", INCLUIR_LINK_BINANCE)
            INTERVALS_ENABLED.update(data.get("INTERVALS_ENABLED", INTERVALS_ENABLED))
            CHANGE_THRESHOLD_BTC_ETH = data.get("CHANGE_THRESHOLD_BTC_ETH", CHANGE_THRESHOLD_BTC_ETH)
            CHANGE_THRESHOLD_DEFAULT = data.get("CHANGE_THRESHOLD_DEFAULT", CHANGE_THRESHOLD_DEFAULT)
            MIN_VOLUME_BY_INTERVAL.update(data.get("MIN_VOLUME_BY_INTERVAL", MIN_VOLUME_BY_INTERVAL))
            MIN_ALERT_INTERVAL_MINUTES = data.get("MIN_ALERT_INTERVAL_MINUTES", MIN_ALERT_INTERVAL_MINUTES)
            INTERVAL_CYCLE_SECONDS.update(data.get("INTERVAL_CYCLE_SECONDS", INTERVAL_CYCLE_SECONDS))
            VOLUMEN_EXTREMO_BY_INTERVAL = data.get("VOLUMEN_EXTREMO_BY_INTERVAL", {})
            FUNDING_CACHE_SECONDS = data.get("FUNDING_CACHE_SECONDS", FUNDING_CACHE_SECONDS)
            DELAY_ALERTAS_MS = data.get("DELAY_ALERTAS_MS", DELAY_ALERTAS_MS)
            DELTA_REPETICION_MINIMA = data.get("DELTA_REPETICION_MINIMA", DELTA_REPETICION_MINIMA)
            ACTUALIZAR_SIMBOLOS_CADA_HORAS = data.get("ACTUALIZAR_SIMBOLOS_CADA_HORAS", ACTUALIZAR_SIMBOLOS_CADA_HORAS)
            PATRONES_ACTIVOS.update(data.get("PATRONES_ACTIVOS", PATRONES_ACTIVOS))
            CALMA_CICLOS_MAX = data.get("CALMA_CICLOS_MAX", CALMA_CICLOS_MAX)            
            ACTIVAR_LONGS = data.get("ACTIVAR_LONGS", True)
            ACTIVAR_SHORTS = data.get("ACTIVAR_SHORTS", True)

            print("✅ Configuración cargada desde config.json")
        except Exception as e:
            print(f"⚠️ Error al cargar configuración: {e}")

def guardar_config():
    data = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "ALERTA_FORMATO_COMPACTO": ALERTA_FORMATO_COMPACTO,
        "INCLUIR_LINK_BINANCE": INCLUIR_LINK_BINANCE,
        "INTERVALS_ENABLED": INTERVALS_ENABLED,
        "CHANGE_THRESHOLD_BTC_ETH": CHANGE_THRESHOLD_BTC_ETH,
        "CHANGE_THRESHOLD_DEFAULT": CHANGE_THRESHOLD_DEFAULT,
        "MIN_VOLUME_BY_INTERVAL": MIN_VOLUME_BY_INTERVAL,
        "MIN_ALERT_INTERVAL_MINUTES": MIN_ALERT_INTERVAL_MINUTES,
        "VOLUMEN_EXTREMO_BY_INTERVAL": VOLUMEN_EXTREMO_BY_INTERVAL,
        "INTERVAL_CYCLE_SECONDS": INTERVAL_CYCLE_SECONDS,
        "SOLO_VELA_CERRADA": SOLO_VELA_CERRADA,
        "DELTA_REPETICION_MINIMA": DELTA_REPETICION_MINIMA,
        "ACTUALIZAR_SIMBOLOS_CADA_HORAS": ACTUALIZAR_SIMBOLOS_CADA_HORAS,
	"FUNDING_CACHE_SECONDS": FUNDING_CACHE_SECONDS,
	"DELAY_ALERTAS_MS": DELAY_ALERTAS_MS,
        "RSI_SOBREVENTA": RSI_SOBREVENTA,
        "RSI_SOBRECOMPRA": RSI_SOBRECOMPRA,
        "ACTIVAR_LONGS": ACTIVAR_LONGS,
        "ACTIVAR_SHORTS": ACTIVAR_SHORTS,
        "PATRONES_ACTIVOS": PATRONES_ACTIVOS,
        "CALMA_CICLOS_MAX": CALMA_CICLOS_MAX,
    }
    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print("✅ Configuración guardada en config.json")
    except Exception as e:
        print(f"⚠️ Error al guardar configuración: {e}")



def should_send_alert(symbol, interval, new_change):
    key = f"{symbol}-{interval}"
    now = datetime.now(timezone.utc)

    last_time = last_alert_times.get(key)
    last_change = last_change_values.get(key)

    # Primera vez
    if last_time is None:
        last_alert_times[key] = now
        last_change_values[key] = new_change
        return True

    # Si no pasó el tiempo mínimo → no hacer nada
    if (now - last_time) < timedelta(minutes=MIN_ALERT_INTERVAL_MINUTES):
        return False

    # Si pasó el tiempo, ahora evaluar delta
    if DELTA_REPETICION_MINIMA is not None and last_change is not None:
        if abs(new_change - last_change) >= DELTA_REPETICION_MINIMA:
            last_alert_times[key] = now
            last_change_values[key] = new_change
            return True

    return False


def formato(update: Update, context: CallbackContext):
    if not context.args:
        estado = "COMPACTO" if ALERTA_FORMATO_COMPACTO else "COMPLETO"
        update.effective_message.reply_text(f"📄 Formato actual: {estado}")
        return
    arg = context.args[0].lower()
    if arg == "b":
        ALERTA_FORMATO_COMPACTO = True
        guardar_config()
        update.effective_message.reply_text("✅ Formato cambiado a COMPACTO.")
    elif arg == "a":
        ALERTA_FORMATO_COMPACTO = False
        guardar_config()
        update.effective_message.reply_text("✅ Formato cambiado a COMPLETO.")
    else:
        update.effective_message.reply_text("❗ Opción no válida. Usa `/formato a` o `/formato b`.")

def estado(update: Update, context: CallbackContext):
    now = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).strftime("%H:%M:%S")
    formato = "COMPACTO" if ALERTA_FORMATO_COMPACTO else "COMPLETO"
    activos = ", ".join([k for k, v in INTERVALS_ENABLED.items() if v])
    msg = (
        f"📊 Estado actual del bot:\n"
        f"• Formato: {formato}\n"
        f"• Intervalos activos: {activos or 'Ninguno'}\n"
        f"• Umbral BTC/ETH: {CHANGE_THRESHOLD_BTC_ETH}%\n"
        f"• Umbral otros: {CHANGE_THRESHOLD_DEFAULT}%\n"
        f"• Hora AR: {now}"
    )
    update.effective_message.reply_text(msg)


def umbral(update: Update, context: CallbackContext):
    #global CHANGE_THRESHOLD_BTC_ETH, CHANGE_THRESHOLD_DEFAULT
#    if not context.args:
#        update.effective_message.reply_text(
#            f"📊 Umbrales actuales:\n"
#            f"• BTC/ETH: {CHANGE_THRESHOLD_BTC_ETH}%\n"
#            f"• Otros símbolos: {CHANGE_THRESHOLD_DEFAULT}%"
#        )
#        return
    if context.args[0].lower() == "a":
        if len(context.args) == 2:
            try:
                new_val = float(context.args[1])
                CHANGE_THRESHOLD_BTC_ETH = new_val
                guardar_config()
                update.effective_message.reply_text(f"✅ Umbral BTC/ETH actualizado a {new_val}%")
            except ValueError:
                update.effective_message.reply_text("❗ Valor inválido. Usa: /umbral a 0.5")
        else:
            update.effective_message.reply_text(f"📈 Umbral BTC/ETH actual: {CHANGE_THRESHOLD_BTC_ETH}%")
    elif context.args[0].lower() == "b":
        if len(context.args) == 2:
            try:
                new_val = float(context.args[1])
                CHANGE_THRESHOLD_DEFAULT = new_val
                guardar_config()
                update.effective_message.reply_text(f"✅ Umbral para otros símbolos actualizado a {new_val}%")
            except ValueError:
                update.effective_message.reply_text("❗ Valor inválido. Usa: /umbral b 3")
        else:
            update.effective_message.reply_text(f"📉 Umbral para otros símbolos: {CHANGE_THRESHOLD_DEFAULT}%")
    else:
        update.effective_message.reply_text(
            "❗ Opción inválida. Usa:\n"
            "/umbral — ver estado\n"
            "/umbral a 0.5 — cambiar BTC/ETH\n"
            "/umbral b 3 — cambiar otros símbolos"
        )


VALID_INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d","1w"]


def simbolos(update: Update, context: CallbackContext):
    global ACTUALIZAR_SIMBOLOS_CADA_HORAS

    if not context.args:
        if SYMBOLS_PATH.exists():
            try:
                with SYMBOLS_PATH.open("r", encoding="utf-8") as f:
                    symbols = json.load(f)
                mensaje = f"""📦 {len(symbols)} símbolos en caché.
🔁 Actualización automática cada {ACTUALIZAR_SIMBOLOS_CADA_HORAS}h
Usa /simbolos actualizar o /simbolos 24"""
                update.effective_message.reply_text(mensaje)
            except Exception:
                update.effective_message.reply_text("⚠️ Error al leer symbols.json")
        else:
            update.effective_message.reply_text("⚠️ Aún no se ha guardado la lista de símbolos.")
        return

    arg = context.args[0].lower()

    if arg == "actualizar":
        update.effective_message.reply_text("🔄 Actualizando lista de símbolos desde Binance...")
        async def actualizar():
            async with aiohttp.ClientSession() as session:
                nuevos = await get_symbols(session)
                guardar_symbols(nuevos)
                update.effective_message.reply_text(f"✅ Lista actualizada con {len(nuevos)} símbolos.")
        threading.Thread(target=lambda: asyncio.run(actualizar()), daemon=True).start()
        return

    try:
        horas = int(arg)
        if horas < 0:
            raise ValueError
        ACTUALIZAR_SIMBOLOS_CADA_HORAS = horas
        guardar_config()
        if horas == 0:
            update.effective_message.reply_text("🚫 Actualización automática desactivada.")
        else:
            update.effective_message.reply_text(f"🔁 Actualización automática configurada cada {horas}h")
    except ValueError:
        update.effective_message.reply_text("❗ Argumento no válido. Usa /simbolos, /simbolos actualizar o /simbolos 24")

def alerta(update: Update, context: CallbackContext):
    global MIN_ALERT_INTERVAL_MINUTES, DELTA_REPETICION_MINIMA

#    if not context.args:
#        mensaje = f"""⏱️ Tiempo mínimo entre alertas: {MIN_ALERT_INTERVAL_MINUTES} minutos
#🔁 Delta mínima para re-alertar: {DELTA_REPETICION_MINIMA if DELTA_REPETICION_MINIMA is not None else 'DESACTIVADO'}%"""
#        update.effective_message.reply_text(mensaje)
#        return

    subcmd = context.args[0].lower()

    try:
        if subcmd.replace(".", "", 1).isdigit():
            val = float(subcmd)
            MIN_ALERT_INTERVAL_MINUTES = val
            guardar_config()
            update.effective_message.reply_text(f"✅ Tiempo mínimo actualizado a {val} minutos")

        elif subcmd == "delta" and len(context.args) > 1:
            val = context.args[1].strip().lower()
            if val in ["no", "off", "-", "none"]:
                DELTA_REPETICION_MINIMA = None
                guardar_config()
                update.effective_message.reply_text("🔕 Re-alerta desactivada")
            else:
                num_val = float(val)
                if num_val < 0:
                    raise ValueError
                DELTA_REPETICION_MINIMA = num_val
                guardar_config()
                update.effective_message.reply_text(f"✅ Delta mínima actualizada a {num_val}%")

        else:
            update.effective_message.reply_text("❗ Comando inválido. Usa /alerta, /alerta 3 o /alerta delta 0.4")

    except ValueError:
        update.effective_message.reply_text("❗ Valor inválido. Usa un número positivo o 'no' para desactivar la delta.")

def ciclo(update: Update, context: CallbackContext):
    if not context.args:
        texto = "⏱️ Frecuencia de chequeo por intervalo:\n"
        for k in VALID_INTERVALS:
            segundos = INTERVAL_CYCLE_SECONDS.get(k, "(no definido)")
            texto += f"• {k}: cada {segundos}s\n"
        update.effective_message.reply_text(texto)
        return

    intervalo_input = context.args[0].lower()
    if intervalo_input not in VALID_INTERVALS:
        update.effective_message.reply_text("❗ Intervalo inválido. Usa: 1m, 5m, 1h, 4h")
        return

    if len(context.args) == 1:
        segundos = INTERVAL_CYCLE_SECONDS.get(intervalo_input, "(no definido)")
        update.effective_message.reply_text(f"⏱️ Intervalo {intervalo}: cada {segundos}s")
        return

    try:
        nuevo_valor = int(context.args[1])
        if nuevo_valor < 10:
            raise ValueError
        INTERVAL_CYCLE_SECONDS[intervalo_input] = nuevo_valor
        guardar_config()
        update.effective_message.reply_text(f"✅ Intervalo {intervalo_input} actualizado a {nuevo_valor} segundos")
    except ValueError:
        update.effective_message.reply_text("❗ Valor inválido. Usa: /ciclo 1h 180")

def ayuda(update: Update, context: CallbackContext):
    texto = (
        "🤖 *Guía rápida de uso del bot de alertas*\n\n"
        "⚙️ *Desde el menú podés configurar:*\n"
        "• *Modo Vela Cerrada* – Activa alertas solo al cierre de cada vela.\n"
        "• *Enlace a TradingView* – Incluye o no un link con el gráfico en cada alerta.\n"
        "• *Intervalos activos* – Elegí qué temporalidades monitorear (ej: 1m, 5m, 1h).\n"
        "• *Volumen mínimo* – Fijá un volumen mínimo por intervalo para filtrar alertas.\n"
        "• *Umbral de cambio* – Ajustá cuántos % de cambio se requieren para alertar.\n"
        "• *Re-alerta por delta mínima* – Evita repetir alertas si el cambio no es significativo.\n"
        "• *Frecuencia de chequeo* – Elegí cada cuántos segundos revisar cada intervalo.\n"
        "• *Formato de alerta* – Elegí entre mensajes compactos o detallados.\n"
        "• *Modo de envío* – Alertas agrupadas o una por una con delay.\n"
        "• *Delay entre alertas* – Pausa mínima entre cada envío (en milisegundos).\n"
        "• *Patrones de Velas* – Activá la detección de patrones técnicos de 1 a 6 velas.\n"
        "• *Concurrencia* – Definí cuántas tareas paralelas puede ejecutar el bot.\n"
        "• *Símbolos monitoreados* – Actualizá la lista de símbolos de Binance o programá su frecuencia.\n\n"

        "📊 *Estado actual:*\n"
        "Usá /estado para ver un resumen completo de tu configuración actual.\n\n"

        "🕯️ *Patrones de Velas Soportados:*\n"
        "_(ordenados por cantidad de velas)_\n\n"
        "*1 vela:*\n"
        "• *Doji* – Indecisión del mercado\n"
        "• *Marubozu* – Fuerte convicción sin sombras\n"
        "• *Martillo* – Posible giro alcista\n"
        "• *Martillo Invertido* – Posible giro alcista (reversión)\n"
        "• *Estrella Fugaz* – Posible giro bajista\n"
        "• *Peonza* – Duda o pausa en la tendencia\n\n"

        "*2 velas:*\n"
        "• *Envolvente Alcista* – Reversión alcista\n"
        "• *Envolvente Bajista* – Reversión bajista\n"
        "• *Doble Techo* – Doble techo (bajista)\n"
        "• *Doble Suelo* – Doble piso (alcista)\n"
        "• *Línea Penetrante* – Giro alcista\n"
        "• *Cubierta de Nube Oscura* – Giro bajista\n\n"

        "*3 a 6 velas:*\n"
        "• *Estrella de la Mañana* – Reversión alcista (3 velas)\n"
        "• *Estrella de la Tarde* – Reversión bajista (3 velas)\n"
        "• *Tres Soldados Blancos* – Fuerte impulso alcista (3 velas)\n"
        "• *Tres Cuervos Negros* – Fuerte presión bajista (3 velas)\n"
        "• *Tres Métodos Ascendentes* – Continuación alcista (5 velas)\n"
        "• *Tres Métodos Descendentes* – Continuación bajista (5 velas)\n\n"
    )
    update.message.reply_text(texto, parse_mode="Markdown")


def config(update: Update, context: CallbackContext):
    ahora = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).strftime("%H:%M:%S")
    activos = ", ".join([k for k, v in INTERVALS_ENABLED.items() if v]) or "Ninguno"
    volumenes = ", ".join([f"{k}: {v}M" for k, v in MIN_VOLUME_BY_INTERVAL.items()])
    frecuencias = ", ".join([f"{k}: {v}s" for k, v in INTERVAL_CYCLE_SECONDS.items()])
    texto = f"""<b>⚙️ Configuración actual del bot</b>
• Formato: {'COMPACTO' if ALERTA_FORMATO_COMPACTO else 'COMPLETO'}
• Link a Binance: {'✅' if INCLUIR_LINK_BINANCE else '❌'}
• Modo vela cerrada: {'✅' if SOLO_VELA_CERRADA else '❌'}
• Intervalos activos: {activos}
• Umbral BTC/ETH: {CHANGE_THRESHOLD_BTC_ETH}%
• Umbral otros: {CHANGE_THRESHOLD_DEFAULT}%
• Volúmenes mínimos: {volumenes}
• Tiempos entre alertas: {MIN_ALERT_INTERVAL_MINUTES} minutos
• Frecuencia de chequeo: {frecuencias}
• Hora AR: {ahora}"""
    update.effective_message.reply_text(texto, parse_mode="HTML")

# ===================== MENÚ INTERACTIVO CON SUBMENÚS =====================
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, MessageHandler, Filters

# Estado por usuario para entradas dinámicas
ESTADO_USUARIO = {}  # { chat_id: {"esperando": "umbral_a"} }

# Diccionario de handlers para vistas simples
HANDLERS = {
    "formato": formato,
    "estado": estado,
    "umbral": umbral,
    "alerta": alerta,
    "ciclo": ciclo,
    "simbolos": simbolos,
    "config": config,
    "ayuda": ayuda,
}


def menu(update: Update, context: CallbackContext):

    if not es_admin(update):
        update.effective_message.reply_text(
            "⛔ Acceso restringido. Solo administradores."
        )
        return

    keyboard = [
        [InlineKeyboardButton("🕯️ Patrones de Velas", callback_data='submenu_patrones'),
         InlineKeyboardButton("📦 Volumen", callback_data='submenu_volumen')],
        [InlineKeyboardButton("🔺 Umbral de Cambio", callback_data='submenu_umbral'),
          InlineKeyboardButton("📈 Umbral RSI", callback_data="submenu_rsi")],
        [InlineKeyboardButton("⚙️ Concurrencia", callback_data='submenu_concurrencia'),
         InlineKeyboardButton("⏱️ Intervalo entre alertas", callback_data='submenu_alerta')],
        [InlineKeyboardButton("🔁 Frecuencia de Chequeo", callback_data='submenu_ciclo_intervalos'),
         InlineKeyboardButton("📤 Modo de Envío", callback_data='submenu_envio')],
        [InlineKeyboardButton("📊 Estado del Bot", callback_data='estado'),
        InlineKeyboardButton("🛠️ Configuración +", callback_data='submenu_configuracion')],
        [InlineKeyboardButton("❓ Ayuda", callback_data='ayuda')]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        update.callback_query.edit_message_text("📘 MENU PRINCIPAL", reply_markup=reply_markup)
    else:
        update.effective_message.reply_text("📘 MENU PRINCIPAL", reply_markup=reply_markup)



# ----------------------------------Submenús----------------------------------------------

def manejar_callback(update: Update, context: CallbackContext):
    global ALERTA_FORMATO_COMPACTO, AGRUPAR_ALERTAS, DELAY_ALERTAS_MS, DELTA_REPETICION_MINIMA, MIN_ALERT_INTERVAL_MINUTES
    query = update.callback_query
    query.answer()
    comando = query.data
    chat_id = update.effective_chat.id

    print("ENTRÓ AL CALLBACK:", comando)


    
    if comando == "reiniciar_bot":
        query.edit_message_text("♻️ Reiniciando bot...")
        os.execv(sys.executable, ['python'] + sys.argv)
        return

    if comando == "envio_estado":
        estado_envio = "AGRUPADO" if AGRUPAR_ALERTAS else "INDIVIDUAL"
        query.edit_message_text(f"📋 Estado actual del envío de alertas:\n• Modo: {estado_envio}\n• Delay entre alertas: {DELAY_ALERTAS_MS} ms")
        return

    if comando == "submenu_envio":
        modo = "Grupal" if AGRUPAR_ALERTAS else "Individual"
        keyboard = [
            [InlineKeyboardButton(f"✉️ Envío de Mensajes Actual: {modo}", callback_data='toggle_envio')],
            [InlineKeyboardButton(f"⏱️ Delay entre Envíos: {DELAY_ALERTAS_MS} ms", callback_data='cambiar_delay')],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_menu')],
        ]
        query.edit_message_text("🔁 Modo de envío de alertas", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if comando == "toggle_modo_vela":
        global SOLO_VELA_CERRADA
        SOLO_VELA_CERRADA = not SOLO_VELA_CERRADA
        guardar_config()
        mostrar_configuracion_general(query)
        query.answer("✔️ Modo vela actualizado")
        return

    if comando == "toggle_link":
        global INCLUIR_LINK_BINANCE
        INCLUIR_LINK_BINANCE = not INCLUIR_LINK_BINANCE
        guardar_config()
        mostrar_configuracion_general(query)
        query.answer("✔️ Enlace actualizado")
        return

    if comando == "toggle_longs":
        global ACTIVAR_LONGS
        ACTIVAR_LONGS = not ACTIVAR_LONGS
        guardar_config()
        mostrar_menu_intervalos(query)
        return
    
    if comando == "toggle_shorts":
        global ACTIVAR_SHORTS
        ACTIVAR_SHORTS = not ACTIVAR_SHORTS
        guardar_config()
        mostrar_menu_intervalos(query)
        return

    if comando == "submenu_configuracion":
       mostrar_configuracion_general(query)
       return

    if comando == "submenu_umbral":
        botones = []
        # ===== BTC / ETH =====
        botones.append([InlineKeyboardButton("🟡 ETH / BTC", callback_data="none")])

        fila = []
        for intervalo in ["5m", "1h", "4h", "1d", "1w"]:
            valor = CHANGE_THRESHOLD_BTC_ETH.get(intervalo, 0.0)
            texto = f"[{intervalo}] {valor}%"
            callback = f"cambiar_umbral_btc_{intervalo}"

            fila.append(InlineKeyboardButton(texto, callback_data=callback))

            if len(fila) == 2:
                botones.append(fila)
                fila = []

        if fila:
            botones.append(fila)

        # ===== OTROS =====
        botones.append([InlineKeyboardButton("⚪ Otros Símbolos", callback_data="none")])

        fila = []
        for intervalo in ["5m", "1h", "4h", "1d", "1w"]:
            valor = CHANGE_THRESHOLD_DEFAULT.get(intervalo, 0.0)
            texto = f"[{intervalo}] {valor}%"
            callback = f"cambiar_umbral_default_{intervalo}"

            fila.append(InlineKeyboardButton(texto, callback_data=callback))

            if len(fila) == 2:
                botones.append(fila)
                fila = []

        if fila:
            botones.append(fila)

        botones.append([InlineKeyboardButton("🔙 Volver", callback_data="volver_menu")])

        query.edit_message_text(
            "🔺 Umbral de Cambio por Intervalo",
            reply_markup=InlineKeyboardMarkup(botones)
        )
        return



    if comando == "submenu_volumen":
        keyboard = [
            [InlineKeyboardButton("💰 Volumen mínimo", callback_data="submenu_volumen_minimo")],
            [InlineKeyboardButton("🔥 Volumen extremo", callback_data="submenu_volumen_extremo")],
            [InlineKeyboardButton("🔙 Volver", callback_data="volver_menu")]
        ]
        query.edit_message_text("⚙️ Configuración de Volumen", reply_markup=InlineKeyboardMarkup(keyboard))
        return


    if comando.startswith("cambiar_volumen_") or comando.startswith("cambiar_extremo_"):
        tipo = "minimo" if comando.startswith("cambiar_volumen_") else "extremo"
        intervalo = comando.split("_")[-1]
        ESTADO_USUARIO[chat_id] = {"esperando": f"volumen_{tipo}_{intervalo}"}
        query.edit_message_text(
            f"✏️ Escribí el nuevo volumen *{tipo}* para {intervalo} (en millones)",
            parse_mode="Markdown"
        )
        return

    
    if comando == "submenu_volumen_minimo":
        mostrar_menu_volumen_por_tipo(query, "minimo")
        return

    if comando == "submenu_volumen_extremo":
        mostrar_menu_volumen_por_tipo(query, "extremo")
        return

    if comando == "submenu_alerta":
        delta_texto = f"{DELTA_REPETICION_MINIMA:.2f}%" if DELTA_REPETICION_MINIMA is not None else "Desactivada"
        keyboard = [
            [InlineKeyboardButton(f"⏱️ Tiempo entre Alertas ({MIN_ALERT_INTERVAL_MINUTES}m)", callback_data='cambiar_alerta_tiempo')],
            [InlineKeyboardButton(f"📉 Delta Mínima ({delta_texto})", callback_data='cambiar_alerta_delta')],
            [InlineKeyboardButton("🚫 Desactivar Delta", callback_data='alerta_reset')],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_menu')],
        ]
        query.edit_message_text("⏱️ Intervalo entre Alertas", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if comando == "submenu_funding_cache":
        ESTADO_USUARIO[chat_id] = {"esperando": "funding_cache"}
        query.edit_message_text("✏️ Ingresa el tiempo de caché del funding en segundos (ej. 180):")
        return

    # Cambios que requieren texto
    if comando.startswith("cambiar_umbral_btc_"):
       intervalo = comando.replace("cambiar_umbral_btc_", "")
       ESTADO_USUARIO[chat_id] = {"esperando": f"umbral_btc_{intervalo}"}
       query.edit_message_text(f"✏️ Escribe el nuevo % para ETH/BTC en {intervalo}")
       return

    if comando.startswith("cambiar_umbral_default_"):
       intervalo = comando.replace("cambiar_umbral_default_", "")
       ESTADO_USUARIO[chat_id] = {"esperando": f"umbral_default_{intervalo}"}
       query.edit_message_text(f"✏️ Escribe el nuevo % para Otros en {intervalo}")
       return


    # Formato directo----tiempo y delta estaban con elif
    if comando == "cambiar_alerta_tiempo":
        ESTADO_USUARIO[chat_id] = {"esperando": "alerta_tiempo"}
        query.edit_message_text("✏️ Escribe el nuevo intervalo mínimo entre alertas (minutos):")
        return

    if comando == "cambiar_alerta_delta":
        ESTADO_USUARIO[chat_id] = {"esperando": "alerta_delta"}
        query.edit_message_text("✏️ Escribe el nuevo delta mínimo de cambio (%):")
        return

    
    if comando == "alerta_reset":
        #global MIN_ALERT_INTERVAL_MINUTES, DELTA_REPETICION_MINIMA
      # MIN_ALERT_INTERVAL_MINUTES = 5
        DELTA_REPETICION_MINIMA = None
        guardar_config()
        query.edit_message_text("🔕 Re-Alerta Desactivada")
        return

    if comando == "formato_a":
        ALERTA_FORMATO_COMPACTO = False
        guardar_config()
        query.edit_message_text("✅ Formato cambiado a COMPLETO.")
        return
    if comando == "formato_b":
        ALERTA_FORMATO_COMPACTO = True
        guardar_config()
        query.edit_message_text("✅ Formato cambiado a COMPACTO.")
        return

    if comando == "simbolos":
        horas = ACTUALIZAR_SIMBOLOS_CADA_HORAS
        keyboard = [
            [InlineKeyboardButton("🔄 Actualizar Símbolos Binance", callback_data='simbolos_actualizar')],
            [InlineKeyboardButton(f"🔁 Actualización Automática ({horas}h)", callback_data='simbolos_configurar')],
            [InlineKeyboardButton("🚫 Desactivar Actualización Automática", callback_data='simbolos_0')],
            [InlineKeyboardButton("🔙 Volver", callback_data='submenu_configuracion')]
        ]
        query.edit_message_text("📦 Símbolos Monitoreados", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if comando == "intervalo":
        fake_update = Update(update.update_id, message=query.message)
        context.args = []
        gestionar_intervalo(fake_update, context)
        return

    if comando == "simbolos_actualizar":
        update._effective_message = query.message
        context.args = ["actualizar"]
        simbolos(update, context)
        return

    
    if comando == "simbolos_configurar":
        ESTADO_USUARIO[chat_id] = {"esperando": "simbolos_configurar"}
        query.edit_message_text("✍️ Ingresa cada cuántas horas se deben actualizar los símbolos (ej: 24 o 0 para desactivar):")
        return

    if comando == "simbolos_0":
        update._effective_message = query.message
        context.args = ["0"]
        simbolos(update, context)
        return

    if comando.startswith("toggle_intervalo_"):
        intervalo = comando.replace("toggle_intervalo_", "")
        if intervalo in INTERVALS_ENABLED:
            INTERVALS_ENABLED[intervalo] = not INTERVALS_ENABLED[intervalo]
            guardar_config()
        mostrar_menu_intervalos(query)
        return

    if comando == "intervalo_menu":
        mostrar_menu_intervalos(query) #esto llama al def para ser dinamico con los iconos
        return

    if comando == "submenu_ciclo_intervalos":
        botones = []
        fila = []
        for intervalo in ["1m", "5m", "15m", "1h", "4h", "1d"]:
            segundos = INTERVAL_CYCLE_SECONDS.get(intervalo, "N/A")
            texto = f"{intervalo} ({segundos}s)" if isinstance(segundos, int) else f"{intervalo} (N/A)"
            callback = f"ciclo_intervalo_{intervalo}"
            fila.append(InlineKeyboardButton(texto, callback_data=callback))
            if len(fila) == 2:
                botones.append(fila)
                fila = []
        if fila:
            botones.append(fila)
        botones.append([InlineKeyboardButton("🔙 Volver", callback_data="volver_menu")])
        query.edit_message_text("⏱️ Selecciona un intervalo. Luego escribe: /ciclo [intervalo] [segundos]", reply_markup=InlineKeyboardMarkup(botones))
        return

    if comando.startswith("ciclo_intervalo_"):
        intervalo = comando.split("_")[-1]
        ESTADO_USUARIO[chat_id] = {"esperando": f"ciclo_{intervalo}"}
        query.edit_message_text(f"✍️ Ingresa cuántos segundos usar para {intervalo} (ej. 180)")
        return

        chat_id = update.effective_chat.id
    
    if comando.startswith("cambiar_rsi_"):
        tipo = comando.replace("cambiar_rsi_", "")
        ESTADO_USUARIO[chat_id] = {"esperando": f"rsi_{tipo}"}
        query.edit_message_text(f"✏️ Escribí el nuevo valor para RSI de {tipo} (ej: 70)")
        return

    if comando == "submenu_rsi":
        botones = [
        [InlineKeyboardButton(f"🔺 Sobrecompra: {RSI_SOBRECOMPRA}", callback_data="cambiar_rsi_sobrecompra")],
        [InlineKeyboardButton(f"🔻 Sobreventa: {RSI_SOBREVENTA}", callback_data="cambiar_rsi_sobreventa")],
        [InlineKeyboardButton("🔙 Volver", callback_data="volver_menu")]
        ]
        query.edit_message_text("📈 Umbral RSI:", reply_markup=InlineKeyboardMarkup(botones))
        return

    if comando == "submenu_concurrencia":
        keyboard = [
            [InlineKeyboardButton(f"⚙️ Concurrencia: {MAX_CONCURRENT_TASKS}", callback_data='cambiar_concurrencia')],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_menu')]
        ]
        query.edit_message_text("⚙️ Concurrencia (tareas paralelas)", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if comando == "cambiar_concurrencia":
        ESTADO_USUARIO[chat_id] = {"esperando": "cambiar_concurrencia"}
        query.edit_message_text("✍️ Escribe el nuevo número de tareas paralelas (ej: 15):")
        return

    # Ejecutar comando directo
    if comando in HANDLERS:
        fake_update = Update(update.update_id, message=query.message)
        context.args = []
        HANDLERS[comando](fake_update, context)
    
    if comando == "enviar_individual":
        AGRUPAR_ALERTAS = False
        guardar_config()
        query.edit_message_text("✅ Ahora se enviarán alertas individualmente con pausa.")
        return

    if comando == "enviar_agrupado":
        AGRUPAR_ALERTAS = True
        guardar_config()
        query.edit_message_text("✅ Ahora se agruparán todas las alertas en un solo mensaje.")
        return

    if comando == "cambiar_delay":
        ESTADO_USUARIO[chat_id] = {"esperando": "cambiar_delay"}
        query.edit_message_text("✏️ Escribe el nuevo tiempo entre envíos (en milisegundos, ej: 150)")
        return

    if comando == "submenu_patrones":
        mostrar_submenu_patrones(query)
        return

    if comando.startswith("toggle_patron_"):
        patron = comando.replace("toggle_patron_", "")
        if patron in PATRONES_ACTIVOS:
            PATRONES_ACTIVOS[patron] = not PATRONES_ACTIVOS[patron]
            guardar_config()
        mostrar_submenu_patrones(query)
        return

    if comando == "cambiar_calma":
        ESTADO_USUARIO[chat_id] = {"esperando": "calma_ciclos"}
        query.edit_message_text("✏️ Escribe cuántos ciclos de calma deben aplicarse (ej: 3)")
        return

    if comando == "toggle_envio":
        AGRUPAR_ALERTAS = not AGRUPAR_ALERTAS
        guardar_config()
    
        modo = "Grupal" if AGRUPAR_ALERTAS else "Individual"
        keyboard = [
            [InlineKeyboardButton(f"✉️ Envío de Mensajes Actual: {modo}", callback_data='toggle_envio')],
            [InlineKeyboardButton(f"⏱️ Delay entre Envíos: {DELAY_ALERTAS_MS} ms", callback_data='cambiar_delay')],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_menu')],
        ]
        query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        query.answer("✔️ Modo de envío actualizado")
        return

    if comando == "toggle_formato":
        ALERTA_FORMATO_COMPACTO = not ALERTA_FORMATO_COMPACTO
        guardar_config()
        mostrar_configuracion_general(query)
        query.answer("✔️ Formato actualizado")
        return


    if comando == "volver_menu":
        menu(update, context)
        return
    else:
        query.edit_message_text("❗ Sección no disponible aún.")

#--------------------------------------------------------------------------------------------------
def mostrar_configuracion_general(query):
    icono = "✅ Activado" if SOLO_VELA_CERRADA else "❌ Desactivado"
    link_icono = "✅ Activado" if INCLUIR_LINK_BINANCE else "❌ Desactivado"
    modo = "🅰️ COMPLETO " if not ALERTA_FORMATO_COMPACTO else "🅱️ COMPACTO "

    keyboard = [
        [InlineKeyboardButton("🛠️ Información Completa", callback_data='config')],
        [InlineKeyboardButton(f"📄 Formato Actual: {modo} ", callback_data='toggle_formato')],
        [InlineKeyboardButton(f" 🕯️ Modo Vela Cerrada - {icono} ", callback_data='toggle_modo_vela')],
        [InlineKeyboardButton(f" 🔗 Enlace a TradingView - {link_icono} ", callback_data='toggle_link')],
        [InlineKeyboardButton("⏱️ Intervalos Activos", callback_data="intervalo_menu")],
        [InlineKeyboardButton(f"💸 Cache Funding ({FUNDING_CACHE_SECONDS}s)", callback_data='submenu_funding_cache')],
        [InlineKeyboardButton(f"😌 Modo Calma ({CALMA_CICLOS_MAX} ciclos)", callback_data='cambiar_calma')],
        [InlineKeyboardButton("📦 Símbolos Monitoreados", callback_data='simbolos')],
        [InlineKeyboardButton("♻️ Reiniciar Bot", callback_data='reiniciar_bot')],
        [InlineKeyboardButton("🔙 Volver", callback_data='volver_menu')]
    ]
    query.edit_message_text("🛠️ Configuración General", reply_markup=InlineKeyboardMarkup(keyboard))
#----------------------------------------------------------------------------------------------------

#--------------------------------- Detección de patrones

def mostrar_submenu_patrones(query):
    botones = []

    GRUPOS_PATRONES = {
        "📍 Patrones de 1 vela": [
            "doji", "marubozu", "martillo", "martillo_invertido", "estrella_fugaz", "peonza"
        ],
        "📍 Patrones de 2 velas": [
            "envolvente_alcista", "envolvente_bajista", "doble_techo", "doble_suelo", "linea_penetrante", "nube_oscura"
        ],
        "📍 Patrones de 3 a 6 velas": [
            "estrella_manana", "estrella_tarde", "tres_soldados_blancos", "tres_cuervos_negros", "tres_metodos_ascendentes", "tres_metodos_descendentes"
        ]
    }

    for titulo, lista in GRUPOS_PATRONES.items():
        # Separador de grupo
        botones.append([InlineKeyboardButton(titulo, callback_data="none")])
        fila = []

        for nombre in lista:
            activo = PATRONES_ACTIVOS.get(nombre, False)
            icono = "✅" if activo else "❌"
            nombre_mostrar = nombre.replace("_", " ").title()
            boton = InlineKeyboardButton(f"{icono} {nombre_mostrar}", callback_data=f"toggle_patron_{nombre}")
            fila.append(boton)
            if len(fila) == 2:
                botones.append(fila)
                fila = []
        if fila:
            botones.append(fila)

    botones.append([InlineKeyboardButton("🔙 Volver", callback_data="volver_menu")])
    query.edit_message_text("🕯️ Patrones de Velas - Activar/Desactivar", reply_markup=InlineKeyboardMarkup(botones))



def detectar_patrones(velas):
    patrones_encontrados = []

    def cuerpo_grande(v):
        return abs(v['open'] - v['close']) > (v['high'] - v['low']) * 0.6

    def es_doji(v):
        return abs(v['open'] - v['close']) < (v['high'] - v['low']) * 0.1

    def es_hammer(v):
        cuerpo = abs(v['open'] - v['close'])
        mecha_inferior = min(v['open'], v['close']) - v['low']
        mecha_superior = v['high'] - max(v['open'], v['close'])
        return cuerpo > 0 and mecha_inferior > 2 * cuerpo and mecha_superior < cuerpo

    def es_inverted_hammer(v):
        cuerpo = abs(v['open'] - v['close'])
        mecha_superior = v['high'] - max(v['open'], v['close'])
        mecha_inferior = min(v['open'], v['close']) - v['low']
        return cuerpo > 0 and mecha_superior > 2 * cuerpo and mecha_inferior < cuerpo

    def es_marubozu(v):
        return abs(v['open'] - v['close']) > (v['high'] - v['low']) * 0.9

    def es_spinning_top(v):
        cuerpo = abs(v['open'] - v['close'])
        sombras = v['high'] - v['low']
        return cuerpo < sombras * 0.3

    v1 = dict(zip(['open', 'high', 'low', 'close'], map(float, velas[-1][1:5])))
    v2 = dict(zip(['open', 'high', 'low', 'close'], map(float, velas[-2][1:5])))

    if PATRONES_ACTIVOS.get("doji") and es_doji(v1):
        patrones_encontrados.append("Doji")

    if PATRONES_ACTIVOS.get("hammer") and es_hammer(v1):
        patrones_encontrados.append("Hammer")

    if PATRONES_ACTIVOS.get("inverted_hammer") and es_inverted_hammer(v1):
        patrones_encontrados.append("Inverted Hammer")

    if PATRONES_ACTIVOS.get("shooting_star") and es_inverted_hammer(v1) and v1['close'] < v1['open']:
        patrones_encontrados.append("Shooting Star")

    if PATRONES_ACTIVOS.get("marubozu") and es_marubozu(v1):
        patrones_encontrados.append("Marubozu")

    if PATRONES_ACTIVOS.get("spinning_top") and es_spinning_top(v1):
        patrones_encontrados.append("Spinning Top")

    # Engulfing
    if PATRONES_ACTIVOS.get("bullish_engulfing") and v2['close'] < v2['open'] and v1['close'] > v1['open'] and v1['open'] < v2['close'] and v1['close'] > v2['open']:
        patrones_encontrados.append("Bullish Engulfing")

    if PATRONES_ACTIVOS.get("bearish_engulfing") and v2['close'] > v2['open'] and v1['close'] < v1['open'] and v1['open'] > v2['close'] and v1['close'] < v2['open']:
        patrones_encontrados.append("Bearish Engulfing")

    return patrones_encontrados
#----------------------------------------------------------------------------------------------------

def manejar_respuesta_usuario(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    estado = ESTADO_USUARIO.get(chat_id, {}).get("esperando")
    texto = update.message.text.strip()
    global CALMA_CICLOS_MAX
    if not estado:
        return  # no espera nada

    elif estado == "calma_ciclos":
        try:
            ciclos = int(texto)
            if ciclos < 0 or ciclos > 50:
                raise ValueError           
            CALMA_CICLOS_MAX = ciclos
            guardar_config()
            update.effective_message.reply_text(f"✅ Modo Calma actualizado a {ciclos} ciclos.")
        except:
            update.effective_message.reply_text("❗ Valor inválido. Usa un número entre 0 y 50.")
        ESTADO_USUARIO.pop(chat_id, None)
        return

    elif estado == "cambiar_delay":
        try:
            val = int(texto)
            if val < 0 or val > 10000:
                raise ValueError
            global DELAY_ALERTAS_MS
            DELAY_ALERTAS_MS = val
            guardar_config()
            update.effective_message.reply_text(f"✅ Delay entre alertas actualizado a {val} ms")
            #ESTADO_USUARIO.pop(chat_id, None)
        except:
            update.effective_message.reply_text("❗ Valor inválido. Usa un número entre 0 y 10000")
        return

    elif estado.startswith("volumen_minimo_") or estado.startswith("volumen_extremo_"):
        tipo, intervalo = estado.replace("volumen_", "").split("_")
        try:
            nuevo_valor = float(texto)
            if tipo == "minimo":
                MIN_VOLUME_BY_INTERVAL[intervalo] = nuevo_valor
            else:
                VOLUMEN_EXTREMO_BY_INTERVAL[intervalo] = nuevo_valor
            guardar_config()
            update.effective_message.reply_text(f"✅ Volumen {tipo} para {intervalo} actualizado a {nuevo_valor}M")
        except:
            update.effective_message.reply_text("❗ Valor inválido. Escribí solo un número (ej: 120)")
        ESTADO_USUARIO.pop(chat_id, None)
        return

    elif estado == "funding_cache":
        try:
            val = int(texto)
            if val < 30 or val > 3600:
                raise ValueError
            global FUNDING_CACHE_SECONDS
            FUNDING_CACHE_SECONDS = val
            guardar_config()
            update.effective_message.reply_text(f"✅ Tiempo de cache del funding actualizado a {val} segundos.")
        except:
            update.effective_message.reply_text("❗ Valor inválido. Usa un número entre 30 y 3600.")
        return

    elif estado.startswith("rsi_"):
        tipo = estado.replace("rsi_", "")
        try:
            nuevo_valor = float(texto)
            if tipo == "sobrecompra":
                global RSI_SOBRECOMPRA
                RSI_SOBRECOMPRA = nuevo_valor
            elif tipo == "sobreventa":
                global RSI_SOBREVENTA
                RSI_SOBREVENTA = nuevo_valor
            guardar_config()
            update.effective_message.reply_text(f"✅ RSI {tipo} actualizado a {nuevo_valor}")
        except:
            update.effective_message.reply_text("❗ Valor inválido. Escribí solo un número (ej: 70)")
        ESTADO_USUARIO.pop(chat_id, None)
        return

    elif estado.startswith("ciclo_"):
        try:
            segundos = int(texto)
            if segundos < 10:
                raise ValueError
            intervalo_input = estado.split("_")[1]
            INTERVAL_CYCLE_SECONDS[intervalo_input] = segundos
            guardar_config()
            update.effective_message.reply_text(f"✅ Intervalo {intervalo_input} actualizado a {segundos} segundos")
        except:
            update.effective_message.reply_text("❗ Valor inválido. Debe ser un número entero mayor o igual a 10.")
        return

    elif estado == "simbolos_configurar":
        try:
            horas = int(texto)
            global ACTUALIZAR_SIMBOLOS_CADA_HORAS
            ACTUALIZAR_SIMBOLOS_CADA_HORAS = horas
            guardar_config()
            if horas == 0:
                update.effective_message.reply_text("🚫 Actualización automática desactivada.")
            else:
                update.effective_message.reply_text(f"🔁 Actualización automática configurada cada {horas}h")
        except:
            update.effective_message.reply_text("❗ Valor inválido. Usa un número entero como 24 o 0.")
        return

    if estado == "cambiar_concurrencia":
        try:
            val = int(texto)
            if val < 1 or val > 100:
                raise ValueError
            global MAX_CONCURRENT_TASKS, semaphore
            MAX_CONCURRENT_TASKS = val
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
            update.effective_message.reply_text(f"✅ Concurrencia actualizada a {val} tareas simultáneas")
        except:
            update.effective_message.reply_text("❗ Valor inválido. Usa un número entre 1 y 100")
        ESTADO_USUARIO.pop(chat_id, None)
        return
    try:
        # ================= UMBRAL POR INTERVALO =================

        if estado.startswith("umbral_btc_"):
            intervalo = estado.replace("umbral_btc_", "")
            val = float(texto)
            CHANGE_THRESHOLD_BTC_ETH[intervalo] = val
            guardar_config()
            update.effective_message.reply_text(
                f"✅ Umbral ETH/BTC {intervalo} actualizado a {val}%"
            )

        elif estado.startswith("umbral_default_"):
            intervalo = estado.replace("umbral_default_", "")
            val = float(texto)
            CHANGE_THRESHOLD_DEFAULT[intervalo] = val
            guardar_config()
            update.effective_message.reply_text(
                f"✅ Umbral Otros {intervalo} actualizado a {val}%"
            )


        elif estado == "alerta_tiempo":
            val = float(texto)
            global MIN_ALERT_INTERVAL_MINUTES
            MIN_ALERT_INTERVAL_MINUTES = val
            guardar_config()
            update.effective_message.reply_text(f"✅ Tiempo mínimo actualizado a {val} minutos")

        elif estado == "alerta_delta":
            val = float(texto)
            global DELTA_REPETICION_MINIMA
            DELTA_REPETICION_MINIMA = val
            guardar_config()
            update.effective_message.reply_text(f"✅ Delta mínima de cambio actualizada a {val}%")

        else:
            update.effective_message.reply_text("❗ Entrada no reconocida.")

    except:
        update.effective_message.reply_text("❗ Valor inválido. Intenta nuevamente.")
    # Limpiar estado
    ESTADO_USUARIO.pop(chat_id, None)
    return

def mostrar_menu_volumen_por_tipo(query, tipo):
    botones = []
    fila = []
    if tipo == "minimo":
        data_dict = MIN_VOLUME_BY_INTERVAL
        titulo = "💰 Volumen mínimo por intervalo:"
        callback_prefix = "cambiar_volumen_"
        volver_a = "submenu_volumen"
    else:  # tipo == "extremo"
        data_dict = VOLUMEN_EXTREMO_BY_INTERVAL
        titulo = "🔥 Volumen extremo por intervalo:"
        callback_prefix = "cambiar_extremo_"
        volver_a = "submenu_volumen"

    for intervalo in ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]:
        valor = data_dict.get(intervalo, 0)
        texto = f"[{intervalo}] {valor}M"
        callback = f"{callback_prefix}{intervalo}"
        fila.append(InlineKeyboardButton(texto, callback_data=callback))
        if len(fila) == 2:
            botones.append(fila)
            fila = []
    if fila:
        botones.append(fila)
    botones.append([InlineKeyboardButton("🔙 Volver", callback_data=volver_a)])
    query.edit_message_text(titulo, reply_markup=InlineKeyboardMarkup(botones))

def version(update: Update, context: CallbackContext):
    print(f"[DEBUG] /version recibido de {update.effective_chat.id}")
    update.effective_message.reply_text("🤖 Versión activa: OK")


def concurrencia(update: Update, context: CallbackContext):
    global MAX_CONCURRENT_TASKS, semaphore
    if not context.args:
        update.effective_message.reply_text(f"⚙️ Tareas en paralelo actuales: {MAX_CONCURRENT_TASKS}")
        return
    try:
        nuevo_valor = int(context.args[0])
        if nuevo_valor < 1 or nuevo_valor > 100:
            raise ValueError
        MAX_CONCURRENT_TASKS = nuevo_valor
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
        update.effective_message.reply_text(f"✅ Concurrencia actualizada a {MAX_CONCURRENT_TASKS} tareas simultáneas")
    except ValueError:
        update.effective_message.reply_text("❗ Valor inválido. Usa: /concurrencia 15")


def mostrar_menu_intervalos(query):
    botones = []
    fila = []

    for intervalo in ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]:
        estado = INTERVALS_ENABLED.get(intervalo, False)
        icono = "✅" if estado else "❌"
        texto = f"{icono} {intervalo}"
        callback = f"toggle_intervalo_{intervalo}"
        fila.append(InlineKeyboardButton(texto, callback_data=callback))

        if len(fila) == 2:
            botones.append(fila)
            fila = []

    if fila:
        botones.append(fila)

    # ------------------------------
    # 🔀 CONTROL LONG / SHORT
    # ------------------------------
    botones.append([
        InlineKeyboardButton(
            f"{'✅' if ACTIVAR_LONGS else '❌'} LONG",
            callback_data="toggle_longs"
        ),
        InlineKeyboardButton(
            f"{'✅' if ACTIVAR_SHORTS else '❌'} SHORT",
            callback_data="toggle_shorts"
        )
    ])

    botones.append([
        InlineKeyboardButton("🔙 Volver", callback_data="submenu_configuracion")
    ])

    query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(botones))
    query.answer("✔️ Actualizado")




def agrupar(update: Update, context: CallbackContext):
    global AGRUPAR_ALERTAS
    AGRUPAR_ALERTAS = True
    guardar_config()
    update.effective_message.reply_text("✅ Ahora se agruparán todas las alertas en un solo mensaje.")

def individual(update: Update, context: CallbackContext):
    global AGRUPAR_ALERTAS
    AGRUPAR_ALERTAS = False
    guardar_config()
    update.effective_message.reply_text("✅ Ahora se enviarán alertas individualmente con pausa.")

def delay(update: Update, context: CallbackContext):
    global DELAY_ALERTAS_MS
    if not context.args:
        update.effective_message.reply_text(f"⏱️ Delay actual entre alertas: {DELAY_ALERTAS_MS} ms")
        return
    try:
        val = int(context.args[0])
        if val < 0 or val > 10000:
            raise ValueError
        DELAY_ALERTAS_MS = val
        guardar_config()
        update.effective_message.reply_text(f"✅ Delay entre alertas actualizado a {val} ms")
    except:
        update.effective_message.reply_text("❗ Valor inválido. Usa: /delay 150")

def envio(update: Update, context: CallbackContext):
    estado_envio = "AGRUPADO" if AGRUPAR_ALERTAS else "INDIVIDUAL"
    update.effective_message.reply_text(f"📋 Estado actual del envío de alertas:\n• Modo: {estado_envio}\n• Delay entre alertas: {DELAY_ALERTAS_MS} ms")

def restart_bot(update: Update, context: CallbackContext):
    update.effective_message.reply_text("♻️ Reiniciando bot...")
    os.execv(sys.executable, ['python'] + sys.argv)


def iniciar_comandos():
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("formato", formato, pass_args=True))
    dp.add_handler(CommandHandler("estado", estado))
    dp.add_handler(CommandHandler("umbral", umbral, pass_args=True))
    dp.add_handler(CommandHandler("ayuda", ayuda))
    dp.add_handler(CommandHandler("alerta", alerta, pass_args=True))
    dp.add_handler(CommandHandler("ciclo", ciclo, pass_args=True))
    dp.add_handler(CommandHandler("simbolos", simbolos, pass_args=True))
    dp.add_handler(CommandHandler("config", config))
    dp.add_handler(CommandHandler("menu", menu))
    dp.add_handler(CommandHandler("agrupar", agrupar))
    dp.add_handler(CommandHandler("individual", individual))
    dp.add_handler(CommandHandler("delay", delay, pass_args=True))
    dp.add_handler(CommandHandler("envio", envio))
    dp.add_handler(CommandHandler("concurrencia", concurrencia, pass_args=True))
    dp.add_handler(CallbackQueryHandler(manejar_callback))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, manejar_respuesta_usuario))
    dp.add_handler(CommandHandler("restart", restart_bot))

    threading.Thread(target=updater.start_polling, daemon=True).start()

async def async_safe_request(url, session, symbol=None, max_retries=3):
    for intento in range(max_retries):
        try:
            async with session.get(url, timeout=8) as res:
                if res.status == 429:
                    retry_after = int(res.headers.get("Retry-After", 60))
                    print(f"[RateLimit] Binance limit alcanzado. Esperando {retry_after}s...")
                    await asyncio.sleep(retry_after)
                    continue
                res.raise_for_status()
                return await res.json()
        except aiohttp.ClientResponseError as e:
            print(f"[HTTP Error] {e.status} en {url}")
            break
        except aiohttp.ClientConnectorError as e:
            print(f"[Conexión fallida] {url} → {e}")
        except asyncio.TimeoutError:
            print(f"[Timeout] {url}")
        except Exception as e:
            print(f"[Error inesperado] {url} → {e}")

        espera = 1.5 * (intento + 1) + random.uniform(0.1, 1.0)
        print(f"🔁 Reintentando {url} en {espera:.1f}s...")
        await asyncio.sleep(espera)

    # Solo registrar error si Binance devuelve un error por símbolo inválido (HTTP 400)
    if symbol:
        if isinstance(res, dict) and res.get("code") == -1121:  # símbolo inválido típico de Binance
            registrar_error_symbol(symbol)
    return None

#SYMBOLS_PATH = Path("symbols.json")

def guardar_symbols(symbols):
    try:
        with SYMBOLS_PATH.open("w", encoding="utf-8") as f:
            json.dump(symbols, f)
        print(f"✅ {len(symbols)} símbolos guardados en symbols.json")
    except Exception as e:
        print(f"❌ Error al guardar symbols.json: {e}")

def cargar_symbols():
    if SYMBOLS_PATH.exists():
        try:
            with SYMBOLS_PATH.open("r", encoding="utf-8") as f:
                symbols = json.load(f)
            print(f"📦 {len(symbols)} símbolos cargados desde symbols.json")
            return symbols
        except Exception as e:
            print(f"⚠️ Error al leer symbols.json: {e}")
    return None

def symbols_necesitan_actualizacion(horas=None):
    if horas is None:
        horas = ACTUALIZAR_SIMBOLOS_CADA_HORAS
    if not SYMBOLS_PATH.exists():
        return True
    mod_time = SYMBOLS_PATH.stat().st_mtime
    edad_horas = (time.time() - mod_time) / 3600
    return edad_horas > horas


EXCLUIDOS = {"BTCSTUSDT", "GAIBUSDT"}  # agrega más aquí

async def get_symbols(session):
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    data = await async_safe_request(url, session)

    if not data or "symbols" not in data:
        print("[get_symbols] ❌ No se pudo obtener exchangeInfo desde Binance")
        return []

    symbols = [
        s["symbol"] for s in data["symbols"]
        if (
            s.get("symbol", "").endswith("USDT")
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"  # 👈 ESTA ES LA CLAVE
            and s.get("symbol") not in EXCLUIDOS
        )
    ]
    print(f"[get_symbols] ✅ {len(symbols)} símbolos USDT PERP activos")
    return symbols

#funding_cache = {}
#api_call_count = 0
#api_klines_count = 0
#api_funding_count = 0
#calma_symbols = {}  # {symbol: (sin_alertas, ciclos_restantes)}


async def get_funding_fee(symbol, session):

    global api_funding_count
    api_funding_count += 1
    now = time.time()

    if symbol in funding_cache:
        valor, ts = funding_cache[symbol]
        if now - ts < FUNDING_CACHE_SECONDS:
            return valor

    url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"
    res = await limitado(async_safe_request, url, session, symbol)
    if not res:
        return 0.0

    api_funding_count += 1
    fee = round(float(res.get("lastFundingRate", 0)) * 100, 4)
    funding_cache[symbol] = (fee, now)
    return fee

async def get_data(symbol, interval, session):

    global api_call_count, api_klines_count
    api_call_count += 1
    api_klines_count += 1

  #  api_call_count += 1
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=100"
    res = await limitado(async_safe_request, url, session, symbol)

    if res is None:
        print(f"[{interval}] Error en filtro de {symbol}: No se pudo obtener datos")
        return None
    if len(res) < 2:
        print(f"[{interval}] Error en filtro de {symbol}: Respuesta inválida o incompleta")
        return None

    closes = [float(k[4]) for k in res]
    df = pd.DataFrame({'close': closes})
    rsi = RSIIndicator(df['close']).rsi().iloc[-1]
    macd_calc = MACD(close=df['close'])
    macd = macd_calc.macd().iloc[-1]
    signal = macd_calc.macd_signal().iloc[-1]
    last_close = closes[-1]
    prev_close = closes[-2]
    change = ((last_close - prev_close) / prev_close) * 100
    volume = float(res[-1][7]) * last_close / 1_000_000
    return round(change, 2), round(volume, 2), last_close, prev_close, round(rsi, 2), round(macd, 2), round(signal, 2)

def detectar_divergencia_rsi_macd(closes, highs, lows, rsi_series, interval):
    """
    Detecta divergencias usando pivots simples.
    Ventana adaptable según timeframe.
    """

    # ---- Ventana dinámica según timeframe ----

    if interval == "5m":
        window = 12
        pivot_strength = 2

    elif interval == "1h":
        window = 20
        pivot_strength = 2

    elif interval == "4h":
        window = 30
        pivot_strength = 3

    elif interval == "1d":
        window = 40
        pivot_strength = 4

    else:
        window = 15
        pivot_strength = 2


    if len(closes) < window:
        return None

    highs = highs[-window:]
    lows = lows[-window:]
    rsi_series = rsi_series[-window:]

    pivot_highs = []
    pivot_lows = []


    # ---- Detectar pivots simples ----
    for i in range(pivot_strength, window - pivot_strength):
 
        # Pivot alto fuerte
        if all(highs[i] > highs[i - j] for j in range(1, pivot_strength + 1)) and \
           all(highs[i] > highs[i + j] for j in range(1, pivot_strength + 1)):
            pivot_highs.append(i)

        # Pivot bajo fuerte
        if all(lows[i] < lows[i - j] for j in range(1, pivot_strength + 1)) and \
           all(lows[i] < lows[i + j] for j in range(1, pivot_strength + 1)):
            pivot_lows.append(i)


    # ---- Necesitamos al menos 2 pivots ----
    if len(pivot_highs) >= 2:
        i1, i2 = pivot_highs[-2], pivot_highs[-1]
        
        # 🔹 Evitar pivots demasiado cercanos
        if abs(i2 - i1) < pivot_strength * 2:
            return None

        price1, price2 = highs[i1], highs[i2]
        rsi1, rsi2 = rsi_series.iloc[i1], rsi_series.iloc[i2]
    
        # 🔹 Exigir diferencia mínima real en precio
        price_diff_pct = abs((price2 - price1) / price1 * 100)
        if price_diff_pct < MIN_DIVERGENCE_PRICE_DIFF:
            return None

        if price2 > price1 and rsi2 < rsi1:
            return "Divergencia Bajista RSI"

    if len(pivot_lows) >= 2:
        i1, i2 = pivot_lows[-2], pivot_lows[-1]

        # 🔹 Evitar pivots demasiado cercanos
        if abs(i2 - i1) < pivot_strength * 2:
            return None

        price1, price2 = lows[i1], lows[i2]
        rsi1, rsi2 = rsi_series.iloc[i1], rsi_series.iloc[i2]
   
        # 🔹 Exigir diferencia mínima real en precio
        price_diff_pct = abs((price2 - price1) / price1 * 100)
        if price_diff_pct < MIN_DIVERGENCE_PRICE_DIFF:
            return None


        if price2 < price1 and rsi2 > rsi1:
            return "Divergencia Alcista RSI"
    return None


def es_timeframe_alto(interval):
    return interval in ["4h", "1d", "1w"]

##-----------------------------------------------------------------------------------------------------------------
def send_alert(symbol, interval, change, volume, price, funding, rsi, macd, signal, prev_price, **kwargs):

    tipo = kwargs.get("tipo")
    divergencia = kwargs.get("divergencia")

    inval_price = kwargs.get("inval_price")
    tp1 = kwargs.get("tp1")
    tp2 = kwargs.get("tp2")

    es_entrada_extrema = (
    tipo in ["entrada_extrema_long", "entrada_extrema_short"]
    and inval_price
    and tp1
    and tp2
    )

    texto_gestion = ""
    #linea_gestion = f"\n{texto_gestion}" if texto_gestion else ""

    if es_entrada_extrema:

        #inval_price = kwargs.get("inval_price")
        #tp1 = kwargs.get("tp1")
        #tp2 = kwargs.get("tp2")

        texto_gestion = f"""
━━━━━━━━━━━━━
📌 <b>Gestión sugerida</b>
❌ Invalidación: ${inval_price}
🎯 TP1: ${tp1}
🎯 TP2: ${tp2}
⚖️ Gestión: 1R / 2R
"""
    linea_gestion = f"\n{texto_gestion}" if texto_gestion else ""  #este lo traje de arriba

    base = symbol.replace("USDT", "")
    # ==============================
    # 🔒 CONTROL LONG / SHORT
    # ==============================
    if es_entrada_extrema:

        if tipo == "entrada_extrema_long":
            if not ACTIVAR_LONGS:
                return
            direction = "ENTRADA LONG\n"

        elif tipo == "entrada_extrema_short":
            if not ACTIVAR_SHORTS:
                return
            direction = "ENTRADA SHORT\n"
        encabezado_color = "🚨"

    else:
        if change > 0:  # SHORT normal
            if not ACTIVAR_SHORTS:
                return
            #direction = "SHORT"
        else:  # LONG normal
            if not ACTIVAR_LONGS:
                return
            #direction = "LONG"
        direction = ""
        #encabezado_color = "🟥" if direction == "SHORT" else "🟩"


    emoji_direccion = "📈" if change > 0 else "📉"
    color_direccion = "🟥" if change > 0 else "🟩"
    emoji_fee = "🟢" if funding > 0 else "🔴" if funding < 0 else "⚪"
    now_utc = datetime.now(timezone.utc)
    time_ar = now_utc.astimezone(ZoneInfo("America/Argentina/Buenos_Aires")).strftime("%H:%M")
    time_us = now_utc.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%H:%M")
    link_linea = f"🔗 <a href='https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}.P'>Ver en TradingView</a>" if INCLUIR_LINK_BINANCE else ""

    encabezado_especial = ""
    if symbol == "BTCUSDT":
        encabezado_especial = "🟡 ₿ BITCOIN ALERTA ₿ 🟡\n"
    elif symbol == "ETHUSDT":
        encabezado_especial = "🟣 ⬢ ETHEREUM ALERTA ⬢ 🟣\n"

    # Clasificación RSI
    rsi_estado = ""
    if rsi > RSI_SOBRECOMPRA:
        rsi_estado = " 🔥 Sobrecompra"
    elif rsi < RSI_SOBREVENTA:
        rsi_estado = " 🧊 Sobreventa"
    
    texto_extra = ""
    #atr = kwargs.get("atr")
    divergencia = kwargs.get("divergencia")
    #if atr:
    #    texto_extra += f"\n🌀 ATR: {atr:.4f}"      #saque ATR Volatilidad, da un valor no le veo sentido por ahora
    if divergencia:
        texto_extra += f"\n⚠️ {divergencia}"

    # Texto del patrón (si existe)
    patrones = kwargs.get("patrones")
    texto_patron = f"\n 🕯️ Patrón: {', '.join(patrones)}" if patrones else ""
    
    badge_extrema = "🚨 !ALERTA ENTRADA! 🚨\n" if es_entrada_extrema else "⚡ ALERTA MOVIMIENTO ⚡\n"
    
    if ALERTA_FORMATO_COMPACTO:
        message = f"""{badge_extrema}{encabezado_especial}{color_direccion} <b>{direction}</b> - {base} | {emoji_direccion} {'Subiendo' if change > 0 else 'Bajando'}  
💰 ${price:,.2f} {emoji_direccion} <b>{('+' if change > 0 else '-')}{abs(change):.2f}%</b> ⏱️{interval}  
📦 {volume}M | 💸 {funding:.4f}%{texto_patron}
🧠 RSI: {rsi}{rsi_estado}  
🕒 {time_ar} 🇦🇷 | {time_us} 🇺🇸
{link_linea}"""

    else:
        volumen_extremo = VOLUMEN_EXTREMO_BY_INTERVAL.get(interval, 0)
        icono_extremo = " 🔥 Extremo" if volumen_extremo > 0 and volume >= volumen_extremo else ""
        message = f"""{badge_extrema}{encabezado_especial}{color_direccion} <b>{direction}</b>  🪙{symbol} ━━
{emoji_direccion} Precio {'subiendo' if change > 0 else 'bajando'}   ⏱️<b>{interval}</b>
━━━━━━━━━━━━━
💰 Precio actual: ${price:,.2f}
📉 (desde ${prev_price:,.2f}) <b>{('+' if change > 0 else '-')}{abs(change):.2f}%</b> 
📦 Vol: {volume}M{icono_extremo}
💸 Fee: {funding:.4f}% {emoji_fee}
{texto_patron}
🧠 RSI: {rsi}{rsi_estado}{texto_extra}{linea_gestion}
━━━━━━━━━━━━━
🕒 {time_ar} 🇦🇷 | {time_us} 🇺🇸
{link_linea}"""

    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="HTML", disable_web_page_preview=True)

### 🌀 ATR: {atr}  ---esto para mostrar en alerta ATR volatilidad
### 📊 MACD: {macd} | Signal: {signal}  o usar para {divergencia}  

#-----------------------------------------------------------------------------------------------------------------------
async def monitor_interval(interval, symbols, session):
    global api_call_count, api_klines_count, api_funding_count

    wait_time = INTERVAL_CYCLE_SECONDS[interval]
    min_volume = MIN_VOLUME_BY_INTERVAL[interval]
    print(f"[{interval}] Monitoreando {len(symbols)} símbolos cada {wait_time} segundos...")
    
    while True:
        ciclo_inicio = time.time()
        alert_count = 0
        llamadas_klines = 0
        llamadas_fee = 0
        llamadas_total = 0

        try:
            cache_ciclo = {}

            async def filtro_basico(symbol):
                try:
                    res = None   # 🔹 inicializar

                    # MODO CALMA: si está en calma, saltar
                    if symbol in calma_symbols:
                        _, ciclos_restantes = calma_symbols[symbol]
                        if ciclos_restantes > 0:
                           calma_symbols[symbol] = (True, ciclos_restantes - 1)
                           return None

                    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=100"
                    res = await limitado(async_safe_request, url, session, symbol)
                    nonlocal llamadas_klines, llamadas_total
                    llamadas_klines += 1
                    llamadas_total += 1
                    if not res or len(res) < 2:
                        return None

                    if SOLO_VELA_CERRADA:
                        close_time = int(res[-1][6]) / 1000
                        if time.time() < close_time:
                            return None

                    closes = [float(k[4]) for k in res]
                    highs = [float(k[2]) for k in res]
                    lows = [float(k[3]) for k in res]
                    last_close = closes[-1]
                    prev_close = closes[-2]
                    volume = float(res[-1][7]) * last_close / 1_000_000
                    change = ((last_close - prev_close) / prev_close) * 100
                    if symbol in ["BTCUSDT", "ETHUSDT"]:
                        threshold = CHANGE_THRESHOLD_BTC_ETH.get(interval, 0.0)
                    else:
                        threshold = CHANGE_THRESHOLD_DEFAULT.get(interval, 0.0)

                    # CALCULAMOS INDICADORES
                    df = pd.DataFrame({'high': highs, 'low': lows, 'close': closes})

                    # RSI, MACD, SIGNAL (y series completas para divergencias)
                    rsi_series = RSIIndicator(df['close']).rsi()
                    macd_calc = MACD(close=df['close'])
                    macd_series = macd_calc.macd()
                    signal_series = macd_calc.macd_signal()

                    rsi = rsi_series.iloc[-1]
                    macd = macd_series.iloc[-1]
                    signal = signal_series.iloc[-1]

                    # ATR
                    #from ta.volatility import AverageTrueRange
                    #atr = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range().iloc[-1]


                    # Patrón de velas
                    ultimas_velas = res[-6:]
                    patrones = detectar_patrones(ultimas_velas)
                                           
                    divergencia = None

                    # ==========================================================
                    # 🎯 PRIORIDAD 1 — ENTRADA (Divergencia + RSI extremo)
                    # ==========================================================

                    if ESTRATEGIA_DIVERGENCIA_ACTIVA and interval in INTERVALOS_ENTRADA:

                        if volume < min_volume:
                            return None

                        # Divergencias
                        divergencia = detectar_divergencia_rsi_macd(closes, highs, lows, rsi_series, interval)
                        if divergencia:
                            # LONG
                            if divergencia == "Divergencia Alcista RSI" and rsi <= RSI_SOBREVENTA:

                                min_inval = min(lows[-6:])
                                riesgo = last_close - min_inval
                                tp1 = last_close + riesgo
                                tp2 = last_close + (riesgo * 2)

                                cache_ciclo[symbol] = {
                                    "change": round(change, 2),
                                    "volume": round(volume, 2),
                                    "price": last_close,
                                    "prev_price": prev_close,
                                    "rsi": round(rsi, 2),
                                    "macd": round(macd, 2),
                                    "signal": round(signal, 2),
                                    "divergencia": divergencia,
                                    "patrones": patrones,
                                    "tipo": "entrada_extrema_long",
                                    "inval_price": round(min_inval, 4),
                                    "tp1": round(tp1, 4),
                                    "tp2": round(tp2, 4),
                                }
                                return symbol

                            # SHORT
                            if divergencia == "Divergencia Bajista RSI" and rsi >= RSI_SOBRECOMPRA:

                                max_inval = max(highs[-6:])
                                riesgo = max_inval - last_close
                                tp1 = last_close - riesgo
                                tp2 = last_close - (riesgo * 2)

                                cache_ciclo[symbol] = {
                                    "change": round(change, 2),
                                    "volume": round(volume, 2),
                                    "price": last_close,
                                    "prev_price": prev_close,
                                    "rsi": round(rsi, 2),
                                    "macd": round(macd, 2),
                                    "signal": round(signal, 2),
                                    "divergencia": divergencia,
                                    "patrones": patrones,
                                    "tipo": "entrada_extrema_short",
                                    "inval_price": round(max_inval, 4),
                                    "tp1": round(tp1, 4),
                                    "tp2": round(tp2, 4),
                                }
                                return symbol

                    # ==========================================================
                    # ⚡ PRIORIDAD 2 — MOVIMIENTO (% cambio)
                    # ==========================================================

                    if ESTRATEGIA_MOVIMIENTO_ACTIVA and interval in INTERVALOS_MOVIMIENTO:

                        if symbol in ["BTCUSDT", "ETHUSDT"]:
                            threshold = CHANGE_THRESHOLD_BTC_ETH.get(interval, 0.0)
                        else:
                            threshold = CHANGE_THRESHOLD_DEFAULT.get(interval, 0.0)

                        if abs(change) >= threshold and volume >= min_volume and should_send_alert(symbol, interval, change):

                            cache_ciclo[symbol] = {
                                "change": round(change, 2),
                                "volume": round(volume, 2),
                                "price": last_close,
                                "prev_price": prev_close,
                                "rsi": round(rsi, 2),
                                "macd": round(macd, 2),
                                "signal": round(signal, 2),
                                "divergencia": divergencia,
                                "patrones": patrones
                            }
                            return symbol
                    

                    # Limpia modo calma si aplica
                    if symbol in calma_symbols:
                        del calma_symbols[symbol]

                    # Guardar todo
                    cache_ciclo[symbol] = {
                        "change": round(change, 2),
                        "volume": round(volume, 2),
                        "price": last_close,
                        "prev_price": prev_close,
                        "rsi": round(rsi, 2),
                        "macd": round(macd, 2),
                        "signal": round(signal, 2),
                        #"atr": round(atr, 4),
                        "divergencia": divergencia,
                        "patrones": patrones
                    }
                    return None  #return symbol

                except Exception as e:
                    print(f"[{interval}] Error en filtro de {symbol}: {e}")
                    return None
  
            candidatos = await asyncio.gather(*(filtro_basico(s) for s in symbols))
            candidatos = [s for s in candidatos if s]

            async def obtener_y_alertar(symbol):
                nonlocal llamadas_fee, llamadas_total

                try:
                     datos = cache_ciclo[symbol]

                     # Solo si pasó el filtro y vamos a alertar, pedimos el funding fee
                     funding = await limitado(get_funding_fee, symbol, session)
                     llamadas_fee += 1
                     llamadas_total += 1

                     send_alert(
                         symbol, interval,
                         change=datos["change"],
                         volume=datos["volume"],
                         price=datos["price"],
                         prev_price=datos["prev_price"],
                         rsi=datos["rsi"],
                         macd=datos["macd"],
                         signal=datos["signal"],
                         funding=funding,
                         patrones=datos.get("patrones", []),
                         #atr=datos.get("atr"),
                         divergencia=datos.get("divergencia"),
                         tipo=datos.get("tipo"),
                         inval_price=datos.get("inval_price"),
                         tp1=datos.get("tp1"),
                         tp2=datos.get("tp2"),
                     )
                     return 1

                except Exception as e:
                     print(f"[{interval}] ❌ Error en símbolo {symbol}: {e}")
                     return 0


            resultados = await asyncio.gather(*(obtener_y_alertar(s) for s in candidatos))
            alert_count = sum(resultados)

        except Exception as e:
            print(f"[{interval}] ❌ Error general del ciclo: {e}")

        duracion = time.time() - ciclo_inicio

        print(f"[{interval}] {datetime.now().strftime('%H:%M:%S')} | {'✅' if alert_count != 0 else '❄️'} {alert_count} alerta{'s' if alert_count != 1 else ' '} | {'🕒 ' if duracion <= 9.99 else '🕒'} {duracion:.2f}s | 💸 {llamadas_fee} Fee | 🔁{'  ' if llamadas_total < 10 else ' ' if llamadas_total < 100 else ''} {llamadas_total} API")

        await asyncio.sleep(wait_time)

async def main():
    cargar_config()
    iniciar_comandos()
    async with aiohttp.ClientSession() as session:
        symbols = None
        if symbols_necesitan_actualizacion():
            symbols = await get_symbols(session)
            guardar_symbols(symbols)
        else:
            symbols = cargar_symbols()
        now = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).strftime("%H:%M:%S")
        formato = "COMPACTO" if ALERTA_FORMATO_COMPACTO else "COMPLETO"
        activos = ", ".join([k for k, v in INTERVALS_ENABLED.items() if v])
        print("   ")
        print("✅━━━━━━━━━━━━━━━━━ INICIANDO BOT ━━━━━━━━━━━━━━━━━✅")
        print(f"            Hora de inicio AR: {now}")
        print(f"> Formato de alerta: {formato}")
        print(f"> Intervalos activos: {activos}")
        print(f"> Símbolos monitoreados: {len(symbols)}")
        print(f"> 🔍 Buscando cambios de precio cada 1 minuto")
        print(f"> 📊 Alerta si:")
        print(f"  • BTC/ETH ≥ {CHANGE_THRESHOLD_BTC_ETH}%")
        print(f"  • Otros ≥ {CHANGE_THRESHOLD_DEFAULT}%")
        print(f"  • No se espera a que la vela cierre")
        print(" ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("  ")  
        tasks = []
        for interval, enabled in INTERVALS_ENABLED.items():
            if enabled:
                tasks.append(asyncio.create_task(monitor_interval(interval, symbols, session)))
        await asyncio.gather(*tasks)
if __name__ == "__main__":
    
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            print("💥 Error fatal en main():", e)
            traceback.print_exc()
            print("🔁 Reiniciando en 10 segundos...")
            time.sleep(10)


