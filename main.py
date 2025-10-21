import asyncio
from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from collections import deque
import uvicorn
# Importa as constantes necessárias do simulator
from simulator import (
    SensorSimulator, LIMITE_CHUVA_72H, UMIDADE_BASE_2M5, UMIDADE_SATURACAO,
    UMIDADE_BASE_1M, UMIDADE_BASE_2M
)
from contextlib import asynccontextmanager
import os
import datetime
from datetime import timezone
import pandas as pd
import numpy as np
import math
import httpx # Para requisições de API assíncronas
import json # Para formatar o log do payload
import uuid

# --- Dash/Plotly Imports ---
import dash
from dash import dcc, html, Input, Output
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
data_store = deque(maxlen=MAX_PONTOS_DADOS)
simulator = SensorSimulator() # Instância inicial
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
    # ... (Seu código de envio de e-mail aqui) ...
    pass

# --- [VERSÃO FINAL] Função de Envio de SMS (usando método do server.py) ---
async def send_sms_alert_async(message):
    """ Envia um SMS de alerta usando a API v2 da Comtele via form-urlencoded. """
    print(f"--- LOG DE SMS (FUNÇÃO INVOCADA) ---")
    print(f"Mensagem: {message}")

    phone_to_send = NOTIFICATION_PHONE
    sender_name = COMTELE_SENDER_ID # Agora é um nome, ex: "RiskGeo"

    if not all([phone_to_send, COMTELE_API_KEY, sender_name]):
        print("ERRO DE SMS: Variáveis (NOTIFICATION_PHONE, COMTELE_API_KEY, COMTELE_SENDER_ID) não configuradas.")
        print("-----------------------------------")
        return

    # A API da Comtele espera o número no formato sem '+' — garantimos isso aqui
    if phone_to_send.startswith('+'):
        phone_to_send = phone_to_send[1:]

    api_url = "https://sms.comtele.com.br/api/v2/send"
    headers = {
        "auth-key": COMTELE_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded"
    }

    # Monta o payload como dados de formulário (igual ao server.py)
    payload = {
        "Sender": str(sender_name),
        "Receivers": str(phone_to_send),
        "Content": str(message)
    }

    print(f"Payload (Form Data) a ser enviado para a API SMS: {payload}")

    try:
        async with httpx.AsyncClient() as client:
            print("LOG DE SMS: Enviando requisição (Form Data) para a API da Comtele...")
            # Usa 'data=payload' em vez de 'json=payload'
            response = await client.post(api_url, headers=headers, data=payload, timeout=15.0)

            print(f"LOG DE SMS: Resposta recebida. Status Code: {response.status_code}")
            response_text = response.text
            print(f"LOG DE SMS: Resposta da API (texto): {response_text}")

            # Tenta decodificar a resposta como JSON para checar o sucesso
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
                if len(data_store) > 0: c_deprec = data_store[-1]["precipitacao_acumulada_mm"]
                else: c_deprec = 0.0
                novo_dado = simulator.gerar_novo_dado( c_deprec, simulated_time_utc, list(data_store) )
                data_store.append(novo_dado)
                simulated_time_utc += datetime.timedelta(minutes=10)
            await asyncio.sleep(INTERVALO_ATUALIZACAO_BACKEND_SEG)
        except asyncio.CancelledError:
            print("LOG SIMULADOR: Tarefa 'rodar_simulador' cancelada.")
            break
        except Exception as e:
            print(f"ERRO na tarefa 'rodar_simulador': {e}")
            await asyncio.sleep(30)

# --- Tarefa de Monitoramento de Alertas (para E-mail e SMS) ---
async def monitorar_alertas():
    global global_last_rain_alert_level, global_last_soil_alert_level
    print("LOG MONITOR: Tarefa 'monitorar_alertas' iniciada.")
    while True:
        try:
            await asyncio.sleep(INTERVALO_MONITOR_ALERTA_SEG)
            data = list(data_store)
            if not data: continue

            # 1. Lógica de Alerta de Chuva
            rain_alert_level = "Livre"; accumulated_72h = 0.0
            if len(data) > 0:
                df_temp = pd.DataFrame(data)
                if 'timestamp' in df_temp.columns and 'pluviometria_mm' in df_temp.columns:
                    try:
                        df_temp['timestamp'] = pd.to_datetime(df_temp['timestamp'])
                        df_temp.set_index('timestamp', inplace=True)
                        latest_timestamp = df_temp.index[-1]
                        timestamp_72h_ago = latest_timestamp - pd.Timedelta(hours=72)
                        df_last_72h = df_temp[df_temp.index >= timestamp_72h_ago]
                        if not df_last_72h.empty: accumulated_72h = df_last_72h['pluviometria_mm'].sum()
                        if accumulated_72h >= 90: rain_alert_level = "Paralização"
                        elif accumulated_72h >= 70: rain_alert_level = "Alerta"
                        elif accumulated_72h >= 51: rain_alert_level = "Atenção"
                    except Exception as e:
                         print(f"WARN MONITOR: Erro cálculo chuva: {e}")
                         pass

            # 2. Lógica de Alerta de Umidade
            soil_alert_level, _ = calculate_soil_alert(data)

            # 3. Lógica dos Gatilhos de E-mail e SMS
            agora_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            if rain_alert_level == "Paralização" and global_last_rain_alert_level != "Paralização":
                print(f"GATILHO DE ALERTA: Chuva atingiu Paralização ({accumulated_72h:.2f} mm).")
                email_subject = f"[ALERTA DE PARALIZAÇÃO] Chuva - {agora_str}"
                email_body = (f"O monitoramento simulado atingiu o nível de PARALIZAÇÃO por CHUVA.\n\n"
                              f"- Acumulado 72h: {accumulated_72h:.2f} mm\n- Nível Anterior: {global_last_rain_alert_level}\n- Horário: {agora_str}")
                asyncio.create_task(send_email_alert_async(email_subject, email_body))
                sms_message = f"ALERTA PARALIZACAO (Chuva): Acum. 72h={accumulated_72h:.1f}mm. Nivel ant: {global_last_rain_alert_level}. Hora: {agora_str[-5:]}"
                asyncio.create_task(send_sms_alert_async(sms_message[:160]))
            global_last_rain_alert_level = rain_alert_level

            if soil_alert_level == "Livre" and global_last_soil_alert_level != "Livre":
                print(f"GATILHO DE ALERTA: Umidade retornou para Livre.")
                email_subject = f"[NORMALIZADO] Umidade do Solo - {agora_str}"
                email_body = (f"O monitoramento simulado retornou ao nível LIVRE para Umidade do Solo.\n\n"
                              f"- Nível Anterior: {global_last_soil_alert_level}\n- Horário: {agora_str}")
                asyncio.create_task(send_email_alert_async(email_subject, email_body))
                sms_message = f"NORMALIZADO (Umidade Solo): Retornou p/ Livre. Nivel ant: {global_last_soil_alert_level}. Hora: {agora_str[-5:]}"
                asyncio.create_task(send_sms_alert_async(sms_message[:160]))
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
        if len(data_store) > 0: c_deprec = data_store[-1]["precipitacao_acumulada_mm"]
        else: c_deprec = 0.0
        novo_dado = simulator.gerar_novo_dado( c_deprec, simulated_time_utc, list(data_store) )
        data_store.append(novo_dado)
        simulated_time_utc += datetime.timedelta(minutes=10)
    print("Preenchimento inicial concluído.")

    global_task_simulador = asyncio.create_task(rodar_simulador())
    global_task_monitor = asyncio.create_task(monitorar_alertas())
    print("Tarefas de background iniciadas.")

    yield # Aplicação roda

    print("Desligando (lifespan): Cancelando tarefas...")
    tasks_to_cancel = []
    if global_task_simulador: tasks_to_cancel.append(global_task_simulador)
    if global_task_monitor: tasks_to_cancel.append(global_task_monitor)

    if tasks_to_cancel:
        for task in tasks_to_cancel:
            if not task.done(): task.cancel()
        try:
            await asyncio.wait(tasks_to_cancel, timeout=2.0, return_when=asyncio.ALL_COMPLETED)
        except asyncio.TimeoutError:
            print("WARN Lifespan: Timeout esperando tarefas cancelarem no desligamento.")
        except Exception as e:
            print(f"WARN Lifespan: Exceção esperando tarefas cancelarem: {e}")

    global_task_simulador = None
    global_task_monitor = None
    print("Simulador e Monitor parados (lifespan).")

# --- Configuração do App FastAPI ---
app = FastAPI(lifespan=lifespan)

# --- Configuração do App Dash ---
dash_app = dash.Dash( __name__, requests_pathname_prefix='/dashboard/', external_stylesheets=[dbc.themes.BOOTSTRAP] )
dash_app.config.suppress_callback_exceptions = True

# --- Layout do Dash ---
dash_app.layout = dbc.Container([
    html.H1("Monitoramento Simulado", className="text-center my-4"),
    dbc.Row([
        dbc.Col(dbc.Button("<< Voltar ao Mapa", href="/", color="secondary", outline=True, size="sm", external_link=True), width={"size": "auto"}),
        dbc.Col(dbc.Button("Reiniciar Simulação", href="/restart-simulation", color="warning", outline=True, size="sm", external_link=True, className="ms-2"), width={"size": "auto"})
    ], justify="end", className="mb-4"),
    dbc.Row([ dbc.Col( dbc.Card( [ dbc.CardHeader("Nível Operacional (Chuva 72h)", className="text-center fw-bold"), dbc.CardBody(id='rain-alert-display', className="text-center") ] ), width=6, lg=4, className="mb-3 mb-lg-0" ), dbc.Col( dbc.Card( [ dbc.CardHeader("Nível Operacional (Umidade Solo)", className="text-center fw-bold"), dbc.CardBody(id='soil-alert-display', className="text-center") ] ), width=6, lg=4 ), ], className="mb-4 justify-content-center"),
    dbc.Row([ dbc.Col([ dbc.Label("Período:", html_for="periodo-dropdown"), dcc.Dropdown( id='periodo-dropdown', options=[ {'label': f"{h} hora{'s' if h > 1 else ''}", 'value': h} for h in [1, 3, 6, 12, 18, 24, 36, 48, 60, 72, 84, 96]], value=72, clearable=False, style={'marginBottom': '20px'} ) ], width=12, lg=4) ], className="mb-4"),
    dbc.Row([ dbc.Col(dcc.Graph(id='graph-pluviometria'), width=12) ]), dbc.Row([ dbc.Col(dcc.Graph(id='graph-umidade'), width=12) ]),
    dcc.Interval( id='interval-component', interval=INTERVALO_ATUALIZACAO_FRONTEND_MS, n_intervals=0 )
], fluid=True)


# --- Função Auxiliar para Calcular Nível de Umidade ---
def calculate_soil_alert(current_data):
    soil_alert_level = "Livre"; soil_alert_color = "green"
    is_above_1m, is_above_2m, is_above_2m5 = False, False, False
    if current_data:
        current_moisture = current_data[-1]
        current_1m = current_moisture.get('umidade_1m_perc', UMIDADE_BASE_1M)
        current_2m = current_moisture.get('umidade_2m_perc', UMIDADE_BASE_2M)
        current_2m5 = current_moisture.get('umidade_2m5_perc', UMIDADE_BASE_2M5)
        is_above_1m = current_1m >= (UMIDADE_BASE_1M + 5.0)
        is_above_2m = current_2m >= (UMIDADE_BASE_2M + 5.0)
        is_above_2m5 = current_2m5 >= (UMIDADE_BASE_2M5 + 1.0)
    if is_above_1m and is_above_2m and is_above_2m5: soil_alert_level, soil_alert_color = "Paralização", "red"
    elif is_above_2m and is_above_2m5: soil_alert_level, soil_alert_color = "Alerta", "orange"
    elif is_above_1m and is_above_2m: soil_alert_level, soil_alert_color = "Alerta", "orange"
    elif is_above_2m5: soil_alert_level, soil_alert_color = "Atenção", "gold"
    elif is_above_1m: soil_alert_level, soil_alert_color = "Atenção", "gold"
    return soil_alert_level, soil_alert_color

# --- Callback do Dash (Apenas para atualizar o Frontend) ---
@dash_app.callback(
    [Output('graph-pluviometria', 'figure'), Output('graph-umidade', 'figure'),
     Output('rain-alert-display', 'children'), Output('soil-alert-display', 'children')],
    [Input('interval-component', 'n_intervals'), Input('periodo-dropdown', 'value')]
)
def update_graphs(n_intervals, selected_hours):
    fig_pluvia_default = go.Figure(); fig_umidade_default = go.Figure(); rain_alert_default = html.H4("Calculando...", style={'color': 'grey'}); soil_alert_default = html.H4("Calculando...", style={'color': 'grey'})
    data = list(data_store)
    if not data: return fig_pluvia_default, fig_umidade_default, rain_alert_default, soil_alert_default
    df = pd.DataFrame(data)
    if 'timestamp' not in df.columns: return fig_pluvia_default, fig_umidade_default, rain_alert_default, soil_alert_default
    try: df['timestamp'] = pd.to_datetime(df['timestamp']); df.set_index('timestamp', inplace=True)
    except Exception as e: return fig_pluvia_default, fig_umidade_default, rain_alert_default, soil_alert_default
    rain_alert_level, rain_alert_color = "Livre", "green"; accumulated_72h = 0.0
    if not df.empty:
        latest_timestamp = df.index[-1]; timestamp_72h_ago = latest_timestamp - pd.Timedelta(hours=72); df_last_72h = df[df.index >= timestamp_72h_ago]
        if not df_last_72h.empty: accumulated_72h = df_last_72h['pluviometria_mm'].sum()
        if accumulated_72h >= 90: rain_alert_level, rain_alert_color = "Paralização", "red"
        elif accumulated_72h >= 70: rain_alert_level, rain_alert_color = "Alerta", "orange"
        elif accumulated_72h >= 51: rain_alert_level, rain_alert_color = "Atenção", "gold"
    soil_alert_level, soil_alert_color = calculate_soil_alert(data)
    rain_alert_display_content = html.H4(f"{rain_alert_level}", style={'color': rain_alert_color, 'fontWeight': 'bold'})
    soil_alert_display_content = html.H4(f"{soil_alert_level}", style={'color': soil_alert_color, 'fontWeight': 'bold'})
    data_points_to_show = int(selected_hours) * PONTOS_POR_HORA; df_filtered = df.tail(data_points_to_show)
    if df_filtered.empty: return go.Figure(), go.Figure(), rain_alert_display_content, soil_alert_display_content
    max_rain_in_window = df_filtered['pluviometria_mm'].max() if not df_filtered.empty else 0
    if pd.isna(max_rain_in_window): max_rain_in_window = 0
    secondary_yaxis_max = 6 if max_rain_in_window < 5 else math.ceil(max_rain_in_window) + 1
    df_filtered = df_filtered.copy(); df_filtered.loc[:, 'precipitacao_acumulada_recalculada'] = df_filtered['pluviometria_mm'].cumsum()
    fig_pluvia = make_subplots(specs=[[{"secondary_y": True}]])
    fig_pluvia.add_trace( go.Bar(x=df_filtered.index, y=df_filtered['pluviometria_mm'], name='Pluviometria (mm)', marker_color='rgb(55, 83, 109)'), secondary_y=True, ); fig_pluvia.add_trace( go.Scatter(x=df_filtered.index, y=df_filtered['precipitacao_acumulada_recalculada'], name='Precipitação Acumulada (mm)', mode='lines', line=dict(color='rgb(26, 118, 255)')), secondary_y=False, )
    fig_pluvia.update_layout(title_text="Pluviometria Horária", hovermode="x unified", plot_bgcolor='white', paper_bgcolor='white', legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))
    fig_pluvia.update_yaxes(title_text="Precipitação Acumulada (mm)", secondary_y=False, range=[0, LIMITE_CHUVA_72H + 10], showgrid=False, zeroline=False); fig_pluvia.update_yaxes(title_text="Pluviometria (mm)", secondary_y=True, range=[0, secondary_yaxis_max], showgrid=False, zeroline=False)
    try: x_range_pluvia=[df_filtered.index.min(), df_filtered.index.max() + pd.Timedelta(hours=1)]
    except ValueError: x_range_pluvia = None
    fig_pluvia.update_xaxes(title_text="Data e Hora", showgrid=False, zeroline=False, range=x_range_pluvia)
    fig_umidade = go.Figure()
    fig_umidade.add_trace(go.Scatter(x=df_filtered.index, y=df_filtered['umidade_1m_perc'], name='Profundidade 1 m', mode='lines', line=dict(color='#28a745', width=3))); fig_umidade.add_trace(go.Scatter(x=df_filtered.index, y=df_filtered['umidade_2m_perc'], name='Profundidade 2 m', mode='lines', line=dict(color='#ffc107', width=3))); fig_umidade.add_trace(go.Scatter(x=df_filtered.index, y=df_filtered['umidade_2m5_perc'], name='Profundidade 2.5 m', mode='lines', line=dict(color='#dc3545', width=3)))
    fig_umidade.update_layout(title_text="Umidade Volumétrica do Solo", yaxis_title="Umidade Volumétrica (%)", xaxis_title="Data e Hora", hovermode="x unified", yaxis_range=[UMIDADE_BASE_2M5 - 5, UMIDADE_SATURACAO + 5], plot_bgcolor='white', paper_bgcolor='white', legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5))
    try: x_range_umidade=[df_filtered.index.min(), df_filtered.index.max() + pd.Timedelta(hours=1)]
    except ValueError: x_range_umidade = None
    fig_umidade.update_xaxes(showgrid=False, zeroline=False, range=x_range_umidade); fig_umidade.update_yaxes(showgrid=False, zeroline=False)
    return fig_pluvia, fig_umidade, rain_alert_display_content, soil_alert_display_content

# --- Rotas FastAPI ---

@app.get("/", response_class=HTMLResponse)
async def read_map_html():
    html_file_path = os.path.join(os.path.dirname(__file__), "map.html")
    try:
        with open(html_file_path, "r", encoding="utf-8") as f: html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200)
    except FileNotFoundError: return HTMLResponse(content="<h1>Erro 404: Arquivo map.html não encontrado.</h1>", status_code=404)
    except Exception as e: return HTMLResponse(content=f"<h1>Erro ao ler o arquivo: {e}</h1>", status_code=500)

@app.get("/api/risk_data", response_class=JSONResponse)
async def get_risk_data():
    accumulated_72h = 0.0
    current_data = list(data_store)
    if current_data:
        df_temp = pd.DataFrame(current_data)
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
    if level == "Atenção": height_percent = 50
    elif level == "Alerta": height_percent = 75
    elif level == "Paralização": height_percent = 100
    elif level == "Livre": height_percent = 15
    return JSONResponse(content={ "alert_level": level, "alert_color": color, "height_percent": height_percent })

@app.get("/health", response_class=JSONResponse)
async def health_check():
    """Endpoint leve para pingar e checar o status."""
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

    if tasks_to_cancel:
        for task in tasks_to_cancel:
             if not task.done(): task.cancel()
        try:
            await asyncio.wait(tasks_to_cancel, timeout=1.0, return_when=asyncio.ALL_COMPLETED)
        except asyncio.TimeoutError: print("WARN: Timeout esperando tarefas cancelarem no reinício.")
        except Exception as e: print(f"WARN: Exceção esperando tarefas cancelarem no reinício: {e}")
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
        if len(data_store) > 0: c_deprec = data_store[-1]["precipitacao_acumulada_mm"]
        else: c_deprec = 0.0
        novo_dado = simulator.gerar_novo_dado( c_deprec, simulated_time_utc, list(data_store) )
        data_store.append(novo_dado)
        simulated_time_utc += datetime.timedelta(minutes=10)
    print("Preenchimento inicial concluído.")

    print("Reiniciando tarefas de background...")
    global_task_simulador = asyncio.create_task(rodar_simulador())
    global_task_monitor = asyncio.create_task(monitorar_alertas())
    print("--- REINÍCIO CONCLUÍDO ---")

    return RedirectResponse(url="/dashboard/")

app.mount("/dashboard", WSGIMiddleware(dash_app.server))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0"

    print(f"Executando localmente (compatível com Render)...")
    print(f"Acesse o Mapa em http://127.0.0.1:{port}")
    print(f"Acesse o Dashboard em http://127.0.0.1:{port}/dashboard/")

    uvicorn.run("main:app", host=host, port=port, reload=False)

