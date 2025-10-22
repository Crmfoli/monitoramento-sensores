import asyncio
from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from collections import deque
import uvicorn
# Importa as constantes necessárias do simulator
from simulator import (
    SensorSimulator, LIMITE_CHUVA_72H,
    UMIDADE_BASE_3M,
    UMIDADE_SATURACAO,
    UMIDADE_BASE_1M, UMIDADE_BASE_2M
)
from contextlib import asynccontextmanager
import os
import datetime
from datetime import timezone
# Import para o seletor de datas
from datetime import date
import pandas as pd
import numpy as np
import math
import httpx  # Para requisições de API assíncronas
import json  # Para formatar o log do payload
import uuid
# Imports para o PDF em memória
from io import BytesIO
import plotly.io as pio
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch

# --- Dash/Plotly Imports ---
import dash
# Adicionado State para o novo callback
from dash import dcc, html, Input, Output, State
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import dash_bootstrap_components as dbc
from dash_bootstrap_components import Button

# --- Configuração da Aplicação ---
MAX_PONTOS_DADOS = 576
INTERVALO_ATUALIZACAO_BACKEND_SEG = 2
INTERVALO_ATUALIZACAO_FRONTEND_MS = 2000
NUM_DADOS_INICIAIS = 10
PONTOS_POR_HORA = 6
INTERVALO_MONITOR_ALERTA_SEG = 10

# --- Armazenamento de Dados e Simulador ---
data_store = deque()
simulator = SensorSimulator()
simulated_time_utc = None

# --- Variáveis Globais para E-mail e SMS ---
global_last_rain_alert_level = "Livre"
global_last_soil_alert_level = "Livre"

# --- Variáveis Globais para Tarefas Asyncio ---
global_task_simulador = None
global_task_monitor = None

# --- Leitura das Variáveis de Ambiente ---
# E-mail
EMAIL_DESTINATARIO = os.environ.get("NOTIFICATION_EMAIL")
EMAIL_REMETENTE = os.environ.get("SENDER_EMAIL")
SMTP_API_KEY = os.environ.get("SMTP2GO_API_KEY")
# SMS
COMTELE_API_KEY = os.environ.get("COMTELE_API_KEY")
COMTELE_SENDER_ID = os.environ.get("COMTELE_SENDER_ID")
NOTIFICATION_PHONE = os.environ.get("NOTIFICATION_PHONE")

if NOTIFICATION_PHONE and not NOTIFICATION_PHONE.startswith('+'):
    NOTIFICATION_PHONE = '+' + NOTIFICATION_PHONE

# --- Log de Verificação Inicial das Variáveis de Ambiente ---
print("--- LOG DE E-MAIL (INICIALIZAÇÃO) ---")
print(f"EMAIL_DESTINATARIO carregado: {EMAIL_DESTINATARIO}")
print(f"EMAIL_REMETENTE carregado: {EMAIL_REMETENTE}")
print(f"SMTP_API_KEY carregado: {'*' * 10 if SMTP_API_KEY else None}")
print("--------------------------------------")
print("--- LOG DE SMS (INICIALIZAÇÃO) ---")
print(f"NOTIFICATION_PHONE carregado: {NOTIFICATION_PHONE}")
print(f"COMTELE_API_KEY carregado: {'*' * 10 if COMTELE_API_KEY else None}")
print(f"COMTELE_SENDER_ID carregado: {COMTELE_SENDER_ID}")
print("-----------------------------------")


# --- Função Assíncrona de Envio de E-mail com Logs ---
async def send_email_alert_async(subject, body):
    api_key = SMTP_API_KEY
    if not all([api_key, EMAIL_DESTINATARIO, EMAIL_REMETENTE]):
        print("ERRO DE E-MAIL: Variáveis (SMTP_API_KEY, NOTIFICATION_EMAIL, SENDER_EMAIL) não configuradas.")
        return

    api_url = "https://api.smtp2go.com/v3/email/send"
    headers = {
        "Content-Type": "application/json"
    }

    payload = {
        "api_key": api_key,
        "sender": EMAIL_REMETENTE,
        "to": [EMAIL_DESTINATARIO],
        "subject": subject,
        "text_body": body
    }

    try:
        async with httpx.AsyncClient() as client:
            print("LOG DE E-MAIL: Enviando requisição para a API do SMTP2GO...")
            response = await client.post(api_url, headers=headers, json=payload, timeout=15.0)

            print(f"LOG DE E-MAIL: Resposta recebida. Status Code: {response.status_code}")
            try:
                response_data = response.json()
                if response.status_code == 200 and response_data.get("data", {}).get("succeeded", 0) > 0:
                    print("LOG DE E-MAIL (SUCESSO): E-mail enviado com sucesso.")
                else:
                    msg_erro = response_data.get('data', {}).get('failures', 'Falha desconhecida no corpo da resposta')
                    print(f"ERRO DE E-MAIL (FALHA API): {msg_erro}")
            except json.JSONDecodeError:
                print(f"ERRO DE E-MAIL: A resposta da API não foi um JSON válido. Resposta: {response.text}")

    except httpx.ConnectError as e:
        print(f"ERRO DE E-MAIL (EXCEÇÃO - Conexão): Falha ao conectar ao servidor SMTP2GO. {e}")
    except httpx.TimeoutException as e:
        print(f"ERRO DE E-MAIL (EXCEÇÃO - Timeout): A requisição demorou muito (timeout). {e}")
    except Exception as e:
        print(f"ERRO DE E-MAIL (EXCEÇÃO - Geral): Exceção ao tentar enviar: {e}")


# --- [VERSÃO FINAL] Função de Envio de SMS (usando método do server.py) ---
async def send_sms_alert_async(message):
    """ Envia um SMS de alerta usando a API v2 da Comtele via form-urlencoded. """
    print(f"--- LOG DE SMS (FUNÇÃO INVOCADA) ---")
    print(f"Mensagem: {message}")

    phone_to_send = NOTIFICATION_PHONE
    sender_name = COMTELE_SENDER_ID  # Agora é um nome, ex: "RiskGeo"

    if not all([phone_to_send, COMTELE_API_KEY, sender_name]):
        print("ERRO DE SMS: Variáveis (NOTIFICATION_PHONE, COMTELE_API_KEY, COMTELE_SENDER_ID) não configuradas.")
        print("-----------------------------------")
        return

    if phone_to_send.startswith('+'):
        phone_to_send = phone_to_send[1:]

    api_url = "https://sms.comtele.com.br/api/v2/send"
    headers = {
        "auth-key": COMTELE_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    payload = {
        "Sender": str(sender_name),
        "Receivers": str(phone_to_send),
        "Content": str(message)
    }

    print(f"Payload (Form Data) a ser enviado para a API SMS: {payload}")

    try:
        async with httpx.AsyncClient() as client:
            print("LOG DE SMS: Enviando requisição (Form Data) para a API da Comtele...")
            response = await client.post(api_url, headers=headers, data=payload, timeout=15.0)

            print(f"LOG DE SMS: Resposta recebida. Status Code: {response.status_code}")
            response_text = response.text
            print(f"LOG DE SMS: Resposta da API (texto): {response_text}")

            try:
                response_data = response.json()
                if response_data.get("Success", False):
                    print(f"LOG DE SMS (SUCESSO): SMS enviado para {phone_to_send}.")
                else:
                    msg_erro = response_data.get('Message', 'Mensagem de erro não disponível')
                    print(f"ERRO DE SMS (FALHA API - Comtele Reportou Falha): {msg_erro}")
            except json.JSONDecodeError:
                print(f"ERRO DE SMS: A resposta da API não foi um JSON válido. Resposta: {response_text}")

    except httpx.ConnectError as e:
        print(f"ERRO DE SMS (EXCEÇÃO - Conexão): Falha ao conectar ao servidor da Comtele. {e}")
    except httpx.TimeoutException as e:
        print(f"ERRO DE SMS (EXCEÇÃO - Timeout): A requisição demorou muito (timeout). {e}")
    except Exception as e:
        print(f"ERRO DE SMS (EXCEÇÃO - Geral): Exceção ao tentar enviar: {e}")

    print("-----------------------------------")


# --- Lógica do Simulador em Background ---
async def rodar_simulador():
    global simulated_time_utc, simulator
    print("LOG SIMULADOR: Tarefa 'rodar_simulador' iniciada.")
    while True:
        try:
            for _ in range(6):
                last_data = data_store[-1] if data_store else {}
                c_deprec = last_data.get("precipitacao_acumulada_mm", 0.0) if isinstance(last_data, dict) else 0.0
                novo_dado = simulator.gerar_novo_dado(c_deprec, simulated_time_utc, list(data_store))
                if isinstance(novo_dado, dict):
                    data_store.append(novo_dado)
                else:
                    print(f"WARN: Simulador retornou dado inválido: {novo_dado}")
                simulated_time_utc += datetime.timedelta(minutes=10)
            await asyncio.sleep(INTERVALO_ATUALIZACAO_BACKEND_SEG)
        except asyncio.CancelledError:
            print("LOG SIMULADOR: Tarefa 'rodar_simulador' cancelada.")
            break
        except Exception as e:
            print(f"ERRO na tarefa 'rodar_simulador': {e}")
            await asyncio.sleep(30)


# --- Tarefa de Monitoramento de Alertas ---
async def monitorar_alertas():
    global global_last_rain_alert_level, global_last_soil_alert_level
    print("LOG MONITOR: Tarefa 'monitorar_alertas' iniciada.")
    while True:
        try:
            await asyncio.sleep(INTERVALO_MONITOR_ALERTA_SEG)
            data = list(data_store)
            if not data: continue

            rain_alert_level = "Livre";
            accumulated_72h = 0.0
            valid_data = [d for d in data if isinstance(d, dict) and 'timestamp' in d and 'pluviometria_mm' in d]
            if valid_data:
                df_temp = pd.DataFrame(valid_data)
                if 'timestamp' in df_temp.columns and 'pluviometria_mm' in df_temp.columns:
                    try:
                        df_temp['timestamp'] = pd.to_datetime(df_temp['timestamp'])
                        df_temp.set_index('timestamp', inplace=True)
                        latest_timestamp = df_temp.index[-1]
                        timestamp_72h_ago = latest_timestamp - pd.Timedelta(hours=72)
                        df_last_72h = df_temp[df_temp.index >= timestamp_72h_ago]
                        if not df_last_72h.empty: accumulated_72h = df_last_72h['pluviometria_mm'].sum()
                        if accumulated_72h >= 90:
                            rain_alert_level = "Paralização"
                        elif accumulated_72h >= 70:
                            rain_alert_level = "Alerta"
                        elif accumulated_72h >= 51:
                            rain_alert_level = "Atenção"
                    except Exception as e:
                        print(f"WARN MONITOR: Erro cálculo chuva: {e}")
                        pass

            soil_alert_level, _ = calculate_soil_alert(data)

            agora_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            if rain_alert_level == "Paralização" and global_last_rain_alert_level != "Paralização":
                print(f"GATILHO DE ALERTA: Chuva atingiu Paralização ({accumulated_72h:.2f} mm).")
                email_subject = f"[ALERTA DE PARALIZAÇÃO] Chuva - {agora_str}"
                email_body = (f"O monitoramento simulado atingiu o nível de PARALIZAÇÃO por CHUVA.\n\n"
                              f"- Acumulado 72h: {accumulated_72h:.2f} mm\n- Nível Anterior: {global_last_rain_alert_level}\n- Horário: {agora_str}")
                asyncio.create_task(send_email_alert_async(email_subject, email_body))
                sms_message = f"ALERTA PARALIZACAO (Chuva): Acum. 72h={accumulated_72h:.1f}mm. Nivel ant: {global_last_rain_alert_level}. Hora: {agora_str[-5:]}"
                asyncio.create_task(send_sms_alert_async(sms_message[:160]))

            # Novo bloco para Umidade do Solo
            if soil_alert_level in ["Paralização", "Alerta",
                                    "Atenção"] and global_last_soil_alert_level != soil_alert_level:
                if soil_alert_level == "Paralização":
                    print(f"GATILHO DE ALERTA: Umidade atingiu Paralização.")
                    email_subject = f"[ALERTA DE PARALIZAÇÃO] Solo - {agora_str}"
                    email_body = (f"O monitoramento simulado atingiu o nível de PARALIZAÇÃO por UMIDADE DO SOLO.\n\n"
                                  f"- Nível Atual: {soil_alert_level}\n- Nível Anterior: {global_last_soil_alert_level}\n- Horário: {agora_str}")
                    asyncio.create_task(send_email_alert_async(email_subject, email_body))
                    sms_message = f"ALERTA PARALIZACAO (Solo): Umidade atingiu nivel {soil_alert_level}. Nivel ant: {global_last_soil_alert_level}. Hora: {agora_str[-5:]}"
                    asyncio.create_task(send_sms_alert_async(sms_message[:160]))
                elif soil_alert_level == "Alerta":
                    print(f"GATILHO DE ALERTA: Umidade atingiu Alerta.")
                    email_subject = f"[ALERTA] Solo - {agora_str}"
                    email_body = (f"O monitoramento simulado atingiu o nível de ALERTA por UMIDADE DO SOLO.\n\n"
                                  f"- Nível Atual: {soil_alert_level}\n- Nível Anterior: {global_last_soil_alert_level}\n- Horário: {agora_str}")
                    asyncio.create_task(send_email_alert_async(email_subject, email_body))
                    sms_message = f"ALERTA (Solo): Umidade atingiu nivel {soil_alert_level}. Nivel ant: {global_last_soil_alert_level}. Hora: {agora_str[-5:]}"
                    asyncio.create_task(send_sms_alert_async(sms_message[:160]))
                elif soil_alert_level == "Atenção":
                    print(f"GATILHO DE ALERTA: Umidade atingiu Atenção.")
                    # Atenção não costuma gerar SMS/E-mail, mas mantive o log de transição

            # Bloco de Normalização
            if soil_alert_level == "Livre" and global_last_soil_alert_level != "Livre":
                print(f"GATILHO DE ALERTA: Umidade retornou para Livre.")
                email_subject = f"[NORMALIZADO] Umidade do Solo - {agora_str}"
                email_body = (f"O monitoramento simulado retornou ao nível LIVRE para Umidade do Solo.\n\n"
                              f"- Nível Anterior: {global_last_soil_alert_level}\n- Horário: {agora_str}")
                asyncio.create_task(send_email_alert_async(email_subject, email_body))
                sms_message = f"NORMALIZADO (Umidade Solo): Retornou p/ Livre. Nivel ant: {global_last_soil_alert_level}. Hora: {agora_str[-5:]}"
                asyncio.create_task(send_sms_alert_async(sms_message[:160]))

            global_last_rain_alert_level = rain_alert_level
            global_last_soil_alert_level = soil_alert_level


        except asyncio.CancelledError:
            print("LOG MONITOR: Tarefa 'monitorar_alertas' cancelada.")
            break
        except Exception as e:
            print(f"ERRO na tarefa 'monitorar_alertas': {e}")
            await asyncio.sleep(30)


# --- Gerenciador de "Lifespan" do FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global simulated_time_utc, simulator, data_store
    global global_task_simulador, global_task_monitor

    print(f"Iniciando simulador (lifespan)...")
    data_store.clear()
    simulator = SensorSimulator()
    agora = datetime.datetime.now(timezone.utc).replace(second=0, microsecond=0)
    simulated_time_utc = agora - datetime.timedelta(minutes=10 * NUM_DADOS_INICIAIS)
    print(f"Tempo inicial: {simulated_time_utc}")
    print(f"Preenchendo {NUM_DADOS_INICIAIS} dados iniciais...")
    for i in range(NUM_DADOS_INICIAIS):
        last_data = data_store[-1] if data_store else {}
        c_deprec = last_data.get("precipitacao_acumulada_mm", 0.0) if isinstance(last_data, dict) else 0.0
        novo_dado = simulator.gerar_novo_dado(c_deprec, simulated_time_utc, list(data_store))
        if isinstance(novo_dado, dict):
            data_store.append(novo_dado)
        else:
            print(f"WARN (Lifespan): Simulador retornou dado inválido: {novo_dado}")
        simulated_time_utc += datetime.timedelta(minutes=10)
    print("Preenchimento inicial concluído.")

    global_task_simulador = asyncio.create_task(rodar_simulador())
    global_task_monitor = asyncio.create_task(monitorar_alertas())
    print("Tarefas de background iniciadas.")

    yield

    print("Desligando (lifespan): Cancelando tarefas...")
    tasks_to_cancel = []
    if global_task_simulador: tasks_to_cancel.append(global_task_simulador)
    if global_task_monitor: tasks_to_cancel.append(global_task_monitor)
    valid_tasks = [t for t in tasks_to_cancel if t]
    if valid_tasks:
        for task in valid_tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait(valid_tasks, timeout=2.0, return_when=asyncio.ALL_COMPLETED)
        except asyncio.TimeoutError:
            print("WARN Lifespan: Timeout esperando tarefas cancelarem no desligamento.")
        except Exception as e:
            print(f"WARN Lifespan: Exceção esperando tarefas cancelarem: {e}")

    global_task_simulador = None
    global_task_monitor = None
    print("Simulador e Monitor parados (lifespan).")


# --- Configuração do App FastAPI ---
app = FastAPI(lifespan=lifespan)

# --- Configuração do App Dash [REINSERIDO] ---
dash_app = dash.Dash(__name__, requests_pathname_prefix='/dashboard/', external_stylesheets=[dbc.themes.BOOTSTRAP])
dash_app.config.suppress_callback_exceptions = True

# --- Layout do Dash [RESPONSIVO PARA MOBILE] ---
dash_app.layout = dbc.Container([
    html.H1("Monitoramento Simulado", className="text-center my-4"),
    dbc.Row([
        dbc.Col(
            dbc.Button("<< Voltar ao Mapa", href="/", color="secondary", outline=True, size="sm", external_link=True),
            width={"size": "auto", "order": "last"}),  # Colocado por último no mobile
        dbc.Col(dbc.Button("Reiniciar Simulação", href="/restart-simulation", color="warning", outline=True, size="sm",
                           external_link=True, className="ms-2"), width={"size": "auto", "order": "last"})
        # Colocado por último no mobile
    ], justify="end", className="mb-4"),

    dbc.Row([
        # Card Chuva (Ocupa 12 colunas no mobile, 4 no desktop)
        dbc.Col(
            dbc.Card([
                dbc.CardHeader("Nível Operacional (Chuva 72h)", className="text-center fw-bold"),
                dbc.CardBody(id='rain-alert-display', className="text-center")
            ], className="h-100"),
            width=12, lg=4, className="mb-3"
        ),
        # Card Umidade Solo (Ocupa 12 colunas no mobile, 4 no desktop)
        dbc.Col(
            dbc.Card([
                dbc.CardHeader("Nível Operacional (Umidade Solo)", className="text-center fw-bold"),
                dbc.CardBody(id='soil-alert-display', className="text-center")
            ], className="h-100"),
            width=12, lg=4, className="mb-3"
        ),
        # Card Gerar Relatório (Ocupa 12 colunas no mobile, 4 no desktop)
        dbc.Col(
            dbc.Card([
                dbc.CardHeader("Gerar Relatório em PDF", className="text-center fw-bold"),
                dbc.CardBody([
                    dbc.Label("Selecione o período do relatório:", className="d-block text-center"),
                    dcc.DatePickerRange(
                        id='report-date-picker',
                        display_format='DD/MM/YYYY',
                        className="mb-3 w-100"
                    ),
                    dbc.Button("Gerar e Baixar Relatório", id="btn-generate-report", color="primary", n_clicks=0,
                               className="w-100"),
                ]),
            ], outline=True, color="secondary", className="h-100"),
            width=12, lg=4, className="mb-4"
        )
    ], className="mb-4 justify-content-center"),

    # Dropdown de Período
    dbc.Row([
        dbc.Col([
            dbc.Label("Período (Gráficos):", html_for="periodo-dropdown"),
            dcc.Dropdown(id='periodo-dropdown',
                         options=[{'label': f"{h} hora{'s' if h > 1 else ''}", 'value': h} for h in
                                  [1, 3, 6, 12, 18, 24, 36, 48, 60, 72, 84, 96]], value=72, clearable=False)
        ], width=12, lg=4)  # Ocupa 12 colunas no mobile, 4 no desktop
    ], className="mb-4"),

    # Gráficos (Ocupam 12 colunas sempre)
    dbc.Row([dbc.Col(dcc.Graph(id='graph-pluviometria'), width=12)]),
    dbc.Row([dbc.Col(dcc.Graph(id='graph-umidade'), width=12)]),

    dcc.Interval(id='interval-main', interval=INTERVALO_ATUALIZACAO_FRONTEND_MS, n_intervals=0),

    dcc.Loading(
        id="loading-report-generator",
        type="default",
        fullscreen=True,
        children=[
            dcc.Download(id="download-pdf-report")
        ]
    )
], fluid=True)


# --- Função Auxiliar para Calcular Nível de Umidade ---
def calculate_soil_alert(current_data):
    soil_alert_level = "Livre";
    soil_alert_color = "green"
    is_above_1m, is_above_2m, is_above_3m = False, False, False
    last_valid_data = None
    if current_data:
        for item in reversed(current_data):
            if isinstance(item, dict):
                last_valid_data = item
                break
    if last_valid_data:
        current_1m = last_valid_data.get('umidade_1m_perc', UMIDADE_BASE_1M)
        current_2m = last_valid_data.get('umidade_2m_perc', UMIDADE_BASE_2M)
        current_3m = last_valid_data.get('umidade_3m_perc', UMIDADE_BASE_3M)
        is_above_1m = current_1m >= (UMIDADE_BASE_1M + 5.0)
        is_above_2m = current_2m >= (UMIDADE_BASE_2M + 5.0)
        is_above_3m = current_3m >= (UMIDADE_BASE_3M + 1.0)

    if is_above_1m and is_above_2m and is_above_3m:
        soil_alert_level, soil_alert_color = "Paralização", "red"
    elif is_above_2m and is_above_3m:
        soil_alert_level, soil_alert_color = "Alerta", "orange"
    elif is_above_1m and is_above_2m:
        soil_alert_level, soil_alert_color = "Alerta", "orange"
    elif is_above_3m:
        soil_alert_level, soil_alert_color = "Atenção", "gold"
    elif is_above_1m:
        soil_alert_level, soil_alert_color = "Atenção", "gold"
    return soil_alert_level, soil_alert_color


# --- Callbacks Separados ---
@dash_app.callback(
    [Output('graph-pluviometria', 'figure'),
     Output('rain-alert-display', 'children'),
     Output('report-date-picker', 'max_date_allowed'),
     Output('report-date-picker', 'min_date_allowed')],
    [Input('interval-main', 'n_intervals'),
     Input('periodo-dropdown', 'value')]
)
def update_rain_and_general_alerts(n_intervals, selected_hours):
    fig_pluvia_default = go.Figure()
    rain_alert_default = html.H4("Calculando...", style={'color': 'grey'})
    today = datetime.date.today()
    default_min_date = date(2020, 1, 1)

    data = list(data_store)
    valid_data = [d for d in data if isinstance(d, dict) and 'timestamp' in d]

    if not valid_data:
        return fig_pluvia_default, rain_alert_default, today, default_min_date

    try:
        latest_date = datetime.datetime.fromisoformat(valid_data[-1]['timestamp']).date()
        earliest_date = datetime.datetime.fromisoformat(valid_data[0]['timestamp']).date()
    except Exception:
        latest_date, earliest_date = today, default_min_date

    df = pd.DataFrame(valid_data)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)

    rain_alert_level, rain_alert_color = "Livre", "green";
    accumulated_72h = 0.0
    latest_timestamp = df.index[-1];
    timestamp_72h_ago = latest_timestamp - pd.Timedelta(hours=72)
    df_last_72h = df[df.index >= timestamp_72h_ago]
    if not df_last_72h.empty: accumulated_72h = df_last_72h['pluviometria_mm'].sum()
    if accumulated_72h >= 90:
        rain_alert_level, rain_alert_color = "Paralização", "red"
    elif accumulated_72h >= 70:
        rain_alert_level, rain_alert_color = "Alerta", "orange"
    elif accumulated_72h >= 51:
        rain_alert_level, rain_alert_color = "Atenção", "gold"
    rain_alert_display_content = html.H4(f"{rain_alert_level}", style={'color': rain_alert_color, 'fontWeight': 'bold'})

    df_filtered = df.tail(int(selected_hours) * PONTOS_POR_HORA)
    if df_filtered.empty:
        return fig_pluvia_default, rain_alert_display_content, latest_date, earliest_date

    max_rain_in_window = df_filtered.get('pluviometria_mm', pd.Series(dtype=float)).max()
    if pd.isna(max_rain_in_window): max_rain_in_window = 0
    secondary_yaxis_max = 6 if max_rain_in_window < 5 else math.ceil(max_rain_in_window) + 1

    df_filtered = df_filtered.copy()
    if 'pluviometria_mm' in df_filtered.columns:
        df_filtered['precipitacao_acumulada_recalculada'] = df_filtered['pluviometria_mm'].cumsum()
    else:
        df_filtered['precipitacao_acumulada_recalculada'] = 0.0

    fig_pluvia = make_subplots(specs=[[{"secondary_y": True}]])
    fig_pluvia.add_trace(go.Bar(x=df_filtered.index, y=df_filtered.get('pluviometria_mm'), name='Pluviometria (mm)',
                                marker_color='rgb(55, 83, 109)'), secondary_y=True)
    fig_pluvia.add_trace(go.Scatter(x=df_filtered.index, y=df_filtered.get('precipitacao_acumulada_recalculada'),
                                    name='Precipitação Acumulada (mm)', mode='lines',
                                    line=dict(color='rgb(26, 118, 255)')), secondary_y=False)
    fig_pluvia.update_layout(title_text="Pluviometria Horária", hovermode="x unified", plot_bgcolor='white',
                             paper_bgcolor='white',
                             legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))
    fig_pluvia.update_yaxes(title_text="Precipitação Acumulada (mm)", secondary_y=False,
                            range=[0, LIMITE_CHUVA_72H + 10], showgrid=False, zeroline=False)
    fig_pluvia.update_yaxes(title_text="Pluviometria (mm)", secondary_y=True, range=[0, secondary_yaxis_max],
                            showgrid=False, zeroline=False)

    return fig_pluvia, rain_alert_display_content, latest_date, earliest_date


@dash_app.callback(
    [Output('graph-umidade', 'figure'),
     Output('soil-alert-display', 'children')],
    [Input('interval-main', 'n_intervals'),
     Input('periodo-dropdown', 'value')]
)
def update_soil_elements(n_intervals, selected_hours):
    fig_umidade_default = go.Figure()
    soil_alert_default = html.H4("Calculando...", style={'color': 'grey'})

    data = list(data_store)
    valid_data = [d for d in data if isinstance(d, dict) and 'timestamp' in d]

    if not valid_data:
        return fig_umidade_default, soil_alert_default

    soil_alert_level, soil_alert_color = calculate_soil_alert(data)
    soil_alert_display_content = html.H4(f"{soil_alert_level}", style={'color': soil_alert_color, 'fontWeight': 'bold'})

    df = pd.DataFrame(valid_data)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)

    df_filtered = df.tail(int(selected_hours) * PONTOS_POR_HORA)
    if df_filtered.empty:
        return fig_umidade_default, soil_alert_display_content

    fig_umidade = go.Figure()
    fig_umidade.add_trace(
        go.Scatter(x=df_filtered.index, y=df_filtered.get('umidade_1m_perc'), name='Profundidade 1 m', mode='lines',
                   line=dict(color='#28a745', width=3)))
    fig_umidade.add_trace(
        go.Scatter(x=df_filtered.index, y=df_filtered.get('umidade_2m_perc'), name='Profundidade 2 m', mode='lines',
                   line=dict(color='#ffc107', width=3)))
    fig_umidade.add_trace(
        go.Scatter(x=df_filtered.index, y=df_filtered.get('umidade_3m_perc'), name='Profundidade 3 m', mode='lines',
                   line=dict(color='#dc3545', width=3)))
    fig_umidade.update_layout(title_text="Umidade Volumétrica do Solo", yaxis_title="Umidade Volumétrica (%)",
                              xaxis_title="Data e Hora", hovermode="x unified",
                              yaxis_range=[UMIDADE_BASE_3M - 5, UMIDADE_SATURACAO + 5],
                              plot_bgcolor='white', paper_bgcolor='white',
                              legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))

    return fig_umidade, soil_alert_display_content


# --- Callback generate_pdf_report ---
@dash_app.callback(
    Output("download-pdf-report", "data"),
    Input("btn-generate-report", "n_clicks"),
    [State("report-date-picker", "start_date"),
     State("report-date-picker", "end_date")],
    prevent_initial_call=True
)
def generate_pdf_report(n_clicks, start_date_str, end_date_str):
    if not n_clicks or not start_date_str or not end_date_str:
        print("Relatório: Data de início ou fim não selecionada.")
        return dash.no_update
    data = list(data_store)
    valid_data = [d for d in data if isinstance(d, dict) and 'timestamp' in d]
    if not valid_data:
        print("Relatório: Sem dados válidos no data_store.")
        return dash.no_update
    df = pd.DataFrame(valid_data)
    try:
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        start_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d').date()
        start_dt = datetime.datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=timezone.utc)
        end_dt = datetime.datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc)
        df_report = df[(df['timestamp'] >= start_dt) & (df['timestamp'] <= end_dt)].copy()
        if df_report.empty:
            print(f"Relatório: Sem dados para o período de {start_dt.date()} a {end_dt.date()}")
            return dash.no_update
    except Exception as e:
        print(f"Erro ao filtrar datas para o relatório: {e}")
        return dash.no_update
    total_rain = df_report.get('pluviometria_mm', pd.Series(dtype=float)).sum()
    max_rain_10min = df_report.get('pluviometria_mm', pd.Series(dtype=float)).max()
    avg_umidade_1m = df_report.get('umidade_1m_perc', pd.Series(dtype=float)).mean()
    avg_umidade_2m = df_report.get('umidade_2m_perc', pd.Series(dtype=float)).mean()
    avg_umidade_3m = df_report.get('umidade_3m_perc', pd.Series(dtype=float)).mean()
    avg_umidade_1m_str = f"{avg_umidade_1m:.2f} %" if not pd.isna(avg_umidade_1m) else "N/D"
    avg_umidade_2m_str = f"{avg_umidade_2m:.2f} %" if not pd.isna(avg_umidade_2m) else "N/D"
    avg_umidade_3m_str = f"{avg_umidade_3m:.2f} %" if not pd.isna(avg_umidade_3m) else "N/D"
    if 'pluviometria_mm' in df_report.columns:
        df_report.loc[:, 'precipitacao_acumulada_recalculada'] = df_report['pluviometria_mm'].cumsum()
    else:
        df_report.loc[:, 'precipitacao_acumulada_recalculada'] = 0.0
    df_report.set_index('timestamp', inplace=True)
    max_rain_in_window = df_report.get('pluviometria_mm', pd.Series(dtype=float)).max()
    if pd.isna(max_rain_in_window): max_rain_in_window = 0
    secondary_yaxis_max = 6 if max_rain_in_window < 5 else math.ceil(max_rain_in_window) + 1
    title_pluvia = f"Pluviometria de {start_date.strftime('%d/%m')} a {end_date.strftime('%d/%m')}"
    fig_pluvia = make_subplots(specs=[[{"secondary_y": True}]])
    fig_pluvia.add_trace(
        go.Bar(x=df_report.index, y=df_report.get('pluviometria_mm', pd.Series(dtype=float)), name='Pluviometria (mm)',
               marker_color='rgb(55, 83, 109)'), secondary_y=True, )
    fig_pluvia.add_trace(
        go.Scatter(x=df_report.index, y=df_report.get('precipitacao_acumulada_recalculada', pd.Series(dtype=float)),
                   name='Precip. Acumulada (mm)', mode='lines', line=dict(color='rgb(26, 118, 255)')),
        secondary_y=False, )
    fig_pluvia.update_layout(title_text=title_pluvia, plot_bgcolor='white', paper_bgcolor='white')
    fig_pluvia.update_yaxes(title_text="Precip. Acumulada (mm)", secondary_y=False, showgrid=False, zeroline=False)
    fig_pluvia.update_yaxes(title_text="Pluviometria (mm)", secondary_y=True, range=[0, secondary_yaxis_max],
                            showgrid=False, zeroline=False)
    title_umidade = f"Umidade do Solo de {start_date.strftime('%d/%m')} a {end_date.strftime('%d/%m')}"
    fig_umidade = go.Figure()
    fig_umidade.add_trace(go.Scatter(x=df_report.index, y=df_report.get('umidade_1m_perc', pd.Series(dtype=float)),
                                     name='Profundidade 1 m', mode='lines', line=dict(color='#28a745', width=3)))
    fig_umidade.add_trace(go.Scatter(x=df_report.index, y=df_report.get('umidade_2m_perc', pd.Series(dtype=float)),
                                     name='Profundidade 2 m', mode='lines', line=dict(color='#ffc107', width=3)))
    fig_umidade.add_trace(go.Scatter(x=df_report.index, y=df_report.get('umidade_3m_perc', pd.Series(dtype=float)),
                                     name='Profundidade 3 m', mode='lines', line=dict(color='#dc3545', width=3)))
    fig_umidade.update_layout(title_text=title_umidade, yaxis_title="Umidade Volumétrica (%)",
                              yaxis_range=[UMIDADE_BASE_3M - 5, UMIDADE_SATURACAO + 5],
                              plot_bgcolor='white', paper_bgcolor='white')
    fig_umidade.update_yaxes(showgrid=False, zeroline=False)
    img_pluvia_bytes = pio.to_image(fig_pluvia, format='png', width=800, height=450)
    img_umidade_bytes = pio.to_image(fig_umidade, format='png', width=800, height=450)
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=inch, leftMargin=inch, topMargin=inch, bottomMargin=inch)
    story = []
    styles = getSampleStyleSheet()
    story.append(Paragraph("Relatório de Monitoramento dos Sensores", styles['h1']))
    story.append(Spacer(1, 12))
    periodo_str = f"Período de Análise: {start_date.strftime('%d/%m/%Y')} a {end_date.strftime('%d/%m/%Y')}"
    story.append(Paragraph(periodo_str, styles['h3']))
    story.append(Spacer(1, 24))
    story.append(Paragraph("Resumo do Período", styles['h2']))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"• Chuva Total Acumulada: {total_rain:.2f} mm", styles['Normal']))
    story.append(Paragraph(f"• Maior Leitura de Chuva (10 min): {max_rain_10min:.2f} mm", styles['Normal']))
    story.append(Paragraph(f"• Umidade Média (1m): {avg_umidade_1m_str}", styles['Normal']))
    story.append(Paragraph(f"• Umidade Média (2m): {avg_umidade_2m_str}", styles['Normal']))
    story.append(Paragraph(f"• Umidade Média (3m): {avg_umidade_3m_str}", styles['Normal']))
    story.append(Spacer(1, 24))
    story.append(Paragraph("Gráficos de Análise", styles['h2']))
    story.append(Spacer(1, 12))
    story.append(Image(BytesIO(img_pluvia_bytes), width=7 * inch, height=3.9375 * inch))
    story.append(Spacer(1, 12))
    story.append(Image(BytesIO(img_umidade_bytes), width=7 * inch, height=3.9375 * inch))
    doc.build(story)
    buffer.seek(0)
    nome_arquivo = f"Relatorio_Sensores_{start_date_str}_a_{end_date_str}.pdf"
    return dcc.send_bytes(buffer.getvalue(), nome_arquivo)


# --- Rotas FastAPI ---
@app.get("/", response_class=HTMLResponse)
async def read_map_html():
    html_file_path = os.path.join(os.path.dirname(__file__), "map.html")
    try:
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Erro 404: Arquivo map.html não encontrado.</h1>", status_code=404)
    except Exception as e:
        return HTMLResponse(content=f"<h1>Erro ao ler o arquivo: {e}</h1>", status_code=500)


@app.get("/api/risk_data", response_class=JSONResponse)
async def get_risk_data():
    accumulated_72h = 0.0
    current_data = list(data_store)
    valid_data = [d for d in current_data if isinstance(d, dict) and 'timestamp' in d and 'pluviometria_mm' in d]
    if valid_data:
        df_temp = pd.DataFrame(valid_data)
        try:
            df_temp['timestamp'] = pd.to_datetime(df_temp['timestamp'])
            df_temp.set_index('timestamp', inplace=True)
            num_points_72h = 72 * PONTOS_POR_HORA
            df_last_72h_temp = df_temp.tail(num_points_72h)
            if not df_last_72h_temp.empty:
                accumulated_72h = df_last_72h_temp['pluviometria_mm'].sum()
        except Exception as e:
            print(f"Erro ao calcular risco chuva API: {e}")
            accumulated_72h = 0.0
    return JSONResponse(content={"accumulated_72h": round(accumulated_72h, 2)})


@app.get("/api/soil_risk_data", response_class=JSONResponse)
async def get_soil_risk_data():
    current_data = list(data_store)
    level, color = calculate_soil_alert(current_data)
    height_percent = 0
    if level == "Atenção":
        height_percent = 50
    elif level == "Alerta":
        height_percent = 75
    elif level == "Paralização":
        height_percent = 100
    elif level == "Livre":
        height_percent = 15
    elif level != "Livre":
        level = "Livre"
        color = "grey"
        height_percent = 15
    return JSONResponse(content={"alert_level": level, "alert_color": color, "height_percent": height_percent})


@app.get("/health", response_class=JSONResponse)
async def health_check():
    return JSONResponse(content={"status": "running"}, status_code=200)


@app.get("/restart-simulation")
async def restart_simulation():
    global data_store, simulator, simulated_time_utc
    global global_task_simulador, global_task_monitor
    global global_last_rain_alert_level, global_last_soil_alert_level

    print("--- REINICIANDO SIMULAÇÃO ---")
    print("Cancelando tarefas antigas...")
    tasks_to_cancel = []
    if global_task_simulador: tasks_to_cancel.append(global_task_simulador)
    if global_task_monitor: tasks_to_cancel.append(global_task_monitor)
    valid_tasks = [t for t in tasks_to_cancel if t]
    if valid_tasks:
        for task in valid_tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait(valid_tasks, timeout=1.0, return_when=asyncio.ALL_COMPLETED)
        except asyncio.TimeoutError:
            print("WARN: Timeout esperando tarefas cancelarem no reinício.")
        except Exception as e:
            print(f"WARN: Exceção esperando tarefas cancelarem no reinício: {e}")
    global_task_simulador = None
    global_task_monitor = None

    print("Limpando dados e resetando estado...")
    data_store.clear()
    simulator = SensorSimulator()
    global_last_rain_alert_level = "Livre"
    global_last_soil_alert_level = "Livre"

    agora = datetime.datetime.now(timezone.utc).replace(second=0, microsecond=0)
    simulated_time_utc = agora - datetime.timedelta(minutes=10 * NUM_DADOS_INICIAIS)
    print(f"Novo tempo inicial: {simulated_time_utc}")
    print(f"Preenchendo {NUM_DADOS_INICIAIS} dados iniciais...")
    for i in range(NUM_DADOS_INICIAIS):
        last_data = data_store[-1] if data_store else {}
        c_deprec = last_data.get("precipitacao_acumulada_mm", 0.0) if isinstance(last_data, dict) else 0.0
        novo_dado = simulator.gerar_novo_dado(c_deprec, simulated_time_utc, list(data_store))
        if isinstance(novo_dado, dict):
            data_store.append(novo_dado)
        else:
            print(f"WARN (Restart): Simulador retornou dado inválido: {novo_dado}")
        simulated_time_utc += datetime.timedelta(minutes=10)
    print("Preenchimento inicial concluído.")

    print("Reiniciando tarefas de background...")
    global_task_simulador = asyncio.create_task(rodar_simulador())
    global_task_monitor = asyncio.create_task(monitorar_alertas())
    print("--- REINÍCIO CONCLÍÍDO ---")

    return RedirectResponse(url="/dashboard/")


app.mount("/dashboard", WSGIMiddleware(dash_app.server))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0"

    print(f"Executando localmente (compatível com Render)...")
    print(f"Acesse o Mapa em http://127.0.0.1:{port}")
    print(f"Acesse o Dashboard em http://127.0.0.1:{port}/dashboard/")

    uvicorn.run("main:app", host=host, port=port, reload=False)
