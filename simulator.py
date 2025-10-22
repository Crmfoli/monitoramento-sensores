import random
import datetime

# --- Constantes de Simulação ---

# Níveis base de umidade
UMIDADE_BASE_1M = 28.0
UMIDADE_BASE_2M = 24.0
UMIDADE_BASE_3M = 22.0  # MODIFICADO de 2m5

# Saturação (O platô)
UMIDADE_SATURACAO = 45.0

# Capacidade Máxima de Infiltração
MAX_INFILTRACAO_POR_CICLO_MM = 3.0

# Fatores de Percolação (Atraso e Amortecimento)
FATOR_PERCOLACAO_1M_2M = 0.28
FATOR_PERCOLACAO_2M_3M = 0.10  # MODIFICADO de 2m5

# Fatores de Drenagem
FATOR_DRENAGEM_1M = 0.020
FATOR_DRENAGEM_2M = 0.004
FATOR_DRENAGEM_3M = 0.008  # MODIFICADO de 2m5

# Limites de Tempestade
LIMITE_CHUVA_24H = 85.0
LIMITE_CHUVA_72H = 120.0
LIMIAR_INICIAL_CHUVA_S1 = 50.0

# Constantes do "Motor de Tempestade"
PROB_INICIO_CHUVA = 0.12
INTENSIDADE_MAXIMA_MM = 3.0
CICLOS_PARA_PICO = (3, 8)
CICLOS_PARA_SECAR = (6, 15)


class SensorSimulator:
    def __init__(self):
        # Estado inicial dos sensores
        self.umidade_1m = UMIDADE_BASE_1M
        self.umidade_2m = UMIDADE_BASE_2M
        self.umidade_3m = UMIDADE_BASE_3M  # MODIFICADO de 2m5

        # Buffers para a "Frente de Umidade"
        self.agua_buffer_2m = 0.0
        self.agua_buffer_3m = 0.0  # MODIFICADO de 2m5

        # Estados de controle de tempestade
        self.modo_seca_forcada = False
        self.tempo_fim_seca = None
        self.estado_clima = "SECO"
        self.intensidade_tempestade = 0.0
        self.ciclos_no_estado = 0
        self.duracao_pico_atual = 0
        self.duracao_seca_atual = 0

        # Estados para o limiar de chuva do Sensor 1
        self.acc_rain_since_dry_1m = 0.0
        self.threshold_1m_met = False

        # Flag para manter S1 saturado
        self.manter_saturacao_1m = False

    def _simular_chuva(self, history_data, current_timestamp_utc):
        """
        Simula a chuva usando um motor de 3 estados.
        """
        # (Lógica de chuva inalterada)
        if self.modo_seca_forcada:
            if current_timestamp_utc < self.tempo_fim_seca:
                self.estado_clima = "SECO";
                self.intensidade_tempestade = 0.0;
                self.ciclos_no_estado = 0
                return 0.0
            else:
                self.modo_seca_forcada = False;
                self.tempo_fim_seca = None
        total_chuva_24h = 0.0;
        total_chuva_72h = 0.0
        limite_24h = current_timestamp_utc - datetime.timedelta(hours=24);
        limite_72h = current_timestamp_utc - datetime.timedelta(hours=72)
        for dado in reversed(history_data):
            # Tenta converter o timestamp, pulando se falhar
            try:
                dado_timestamp = datetime.datetime.fromisoformat(dado['timestamp'])
            except (ValueError, TypeError):
                continue  # Pula este dado se o timestamp for inválido

            if dado_timestamp < limite_72h: break
            if dado_timestamp > limite_72h:
                total_chuva_72h += dado.get('pluviometria_mm', 0.0)  # Usa .get() para segurança
                if dado_timestamp > limite_24h:
                    total_chuva_24h += dado.get('pluviometria_mm', 0.0)  # Usa .get() para segurança

        if total_chuva_72h > LIMITE_CHUVA_72H:
            self.estado_clima = "SECO";
            self.intensidade_tempestade = 0.0;
            self.ciclos_no_estado = 0
            self.modo_seca_forcada = True;
            self.tempo_fim_seca = current_timestamp_utc + datetime.timedelta(days=5)
            return 0.0
        elif total_chuva_24h > LIMITE_CHUVA_24H:
            self.estado_clima = "SECO";
            self.intensidade_tempestade = 0.0;
            self.ciclos_no_estado = 0
            self.modo_seca_forcada = True;
            intervalo_seca_minutos = random.randint(180, 360)
            self.tempo_fim_seca = current_timestamp_utc + datetime.timedelta(minutes=intervalo_seca_minutos)
            return 0.0

        chuva_mm = 0.0
        if self.estado_clima == "SECO":
            if random.random() < PROB_INICIO_CHUVA:
                self.estado_clima = "FORMANDO_TEMPESTADE";
                self.ciclos_no_estado = 0;
                self.intensidade_tempestade = 0.0
                self.duracao_pico_atual = random.randint(CICLOS_PARA_PICO[0], CICLOS_PARA_PICO[1])
                self.duracao_seca_atual = random.randint(CICLOS_PARA_SECAR[0], CICLOS_PARA_SECAR[1])
            chuva_mm = 0.0
        elif self.estado_clima == "FORMANDO_TEMPESTADE":
            self.ciclos_no_estado += 1;
            self.intensidade_tempestade = min(1.0, self.ciclos_no_estado / self.duracao_pico_atual)
            chuva_base = self.intensidade_tempestade * INTENSIDADE_MAXIMA_MM;
            chuva_mm = chuva_base * random.uniform(0.7, 1.3)
            if self.ciclos_no_estado >= self.duracao_pico_atual: self.estado_clima = "DIMINUINDO_TEMPESTADE"; self.ciclos_no_estado = 0
        elif self.estado_clima == "DIMINUINDO_TEMPESTADE":
            self.ciclos_no_estado += 1;
            self.intensidade_tempestade = max(0.0, 1.0 - (self.ciclos_no_estado / self.duracao_seca_atual))
            chuva_base = self.intensidade_tempestade * INTENSIDADE_MAXIMA_MM;
            chuva_mm = chuva_base * random.uniform(0.5, 1.1)
            if self.ciclos_no_estado >= self.duracao_seca_atual: self.estado_clima = "SECO"; self.ciclos_no_estado = 0; self.intensidade_tempestade = 0.0
        return round(max(0.0, chuva_mm), 2)

    # Lógica de Umidade com Saturação Condicional de S1
    def _simular_umidade(self, chuva_mm):
        """Calcula a nova umidade com base na chuva e na drenagem."""

        # 1. Drenagem/Secagem (com lag reverso)
        umidade_1m_antes_drenagem = self.umidade_1m

        # Só drena S1 se não estivermos mantendo a saturação
        if not self.manter_saturacao_1m:
            self.umidade_1m -= (self.umidade_1m - UMIDADE_BASE_1M) * FATOR_DRENAGEM_1M

        # S2 e S3 drenam normalmente
        self.umidade_2m -= (self.umidade_2m - UMIDADE_BASE_2M) * FATOR_DRENAGEM_2M
        self.umidade_3m -= (self.umidade_3m - UMIDADE_BASE_3M) * FATOR_DRENAGEM_3M  # MODIFICADO de 2m5

        # Garante que não fique abaixo da base
        self.umidade_1m = max(self.umidade_1m, UMIDADE_BASE_1M)
        self.umidade_2m = max(self.umidade_2m, UMIDADE_BASE_2M)
        self.umidade_3m = max(self.umidade_3m, UMIDADE_BASE_3M)  # MODIFICADO de 2m5

        # Verifica se S1 secou para resetar o limiar de chuva E a flag de manter saturação
        if umidade_1m_antes_drenagem > UMIDADE_BASE_1M + 0.1 and self.umidade_1m <= UMIDADE_BASE_1M + 0.1:
            self.threshold_1m_met = False;
            self.acc_rain_since_dry_1m = 0.0
            self.manter_saturacao_1m = False  # Libera a flag se S1 secou

        # 2. Infiltração
        agua_para_infiltrar_potencial = min(chuva_mm, MAX_INFILTRACAO_POR_CICLO_MM)
        agua_para_infiltrar_efetiva = 0.0

        # Lógica do Limiar de 50mm para S1
        if not self.threshold_1m_met:
            self.acc_rain_since_dry_1m += agua_para_infiltrar_potencial
            if self.acc_rain_since_dry_1m >= LIMIAR_INICIAL_CHUVA_S1:
                self.threshold_1m_met = True
                agua_para_infiltrar_efetiva = agua_para_infiltrar_potencial
        else:
            agua_para_infiltrar_efetiva = agua_para_infiltrar_potencial

        # Zera os buffers ANTES de processar as camadas
        agua_chegando_2m = self.agua_buffer_2m
        agua_chegando_3m = self.agua_buffer_3m  # MODIFICADO de 2m5
        self.agua_buffer_2m = 0.0
        self.agua_buffer_3m = 0.0  # MODIFICADO de 2m5

        # --- Processa Camada 1m ---
        agua_chegando_1m = agua_para_infiltrar_efetiva
        agua_absorvida_1m = 0.0
        agua_excedente_1m = 0.0
        agua_percolada_1m = 0.0

        if self.manter_saturacao_1m:
            self.umidade_1m = UMIDADE_SATURACAO
            agua_excedente_1m = agua_chegando_1m
            agua_percolada_1m = 0.0
            self.agua_buffer_2m = agua_excedente_1m
        else:
            agua_percolada_1m = agua_chegando_1m * FATOR_PERCOLACAO_1M_2M
            agua_potencial_1m = agua_chegando_1m - agua_percolada_1m
            capacidade_1m = max(0, UMIDADE_SATURACAO - self.umidade_1m)
            agua_absorvida_1m = min(agua_potencial_1m, capacidade_1m)
            agua_excedente_1m = agua_potencial_1m - agua_absorvida_1m
            self.umidade_1m += agua_absorvida_1m
            self.agua_buffer_2m = agua_percolada_1m + agua_excedente_1m
            if self.umidade_1m >= UMIDADE_SATURACAO - 0.1:
                self.manter_saturacao_1m = True

        # --- Processa Camada 2m ---
        agua_percolada_lenta_2m = agua_chegando_2m * FATOR_PERCOLACAO_2M_3M  # MODIFICADO de 2m5
        agua_potencial_2m = agua_chegando_2m
        capacidade_2m = max(0, UMIDADE_SATURACAO - self.umidade_2m)
        agua_absorvida_2m = min(agua_potencial_2m, capacidade_2m)
        agua_excedente_2m = agua_potencial_2m - agua_absorvida_2m
        self.umidade_2m += agua_absorvida_2m
        self.agua_buffer_3m = agua_percolada_lenta_2m + agua_excedente_2m  # MODIFICADO de 2m5

        # --- Processa Camada 3m ---  # MODIFICADO de 2m5
        capacidade_3m = max(0, UMIDADE_SATURACAO - self.umidade_3m)  # MODIFICADO de 2m5
        agua_absorvida_3m = min(agua_chegando_3m, capacidade_3m)  # MODIFICADO de 2m5
        self.umidade_3m += agua_absorvida_3m  # MODIFICADO de 2m5

        # Verifica se S2 atingiu saturação para DESATIVAR a flag de S1
        if self.manter_saturacao_1m and self.umidade_2m >= UMIDADE_SATURACAO - 0.1:
            self.manter_saturacao_1m = False

        # Garante que os valores não fiquem abaixo do base ou acima da saturação final
        self.umidade_1m = max(UMIDADE_BASE_1M, min(self.umidade_1m, UMIDADE_SATURACAO))
        self.umidade_2m = max(UMIDADE_BASE_2M, min(self.umidade_2m, UMIDADE_SATURACAO))
        self.umidade_3m = max(UMIDADE_BASE_3M, min(self.umidade_3m, UMIDADE_SATURACAO))  # MODIFICADO de 2m5

    def gerar_novo_dado(self, acumulado_anterior_DEPRECATED, timestamp_utc, history_data):
        """Função principal: Gera um novo conjunto de dados de 10 min."""
        chuva_mm = self._simular_chuva(history_data, timestamp_utc)
        self._simular_umidade(chuva_mm)

        # Calcula novo acumulado de forma segura
        novo_acumulado = 0.0
        if history_data:
            # Pega o último dado válido do histórico
            ultimo_dado_valido = None
            for dado in reversed(history_data):
                if isinstance(dado, dict) and 'precipitacao_acumulada_mm' in dado:
                    ultimo_dado_valido = dado
                    break
            if ultimo_dado_valido:
                novo_acumulado = ultimo_dado_valido.get('precipitacao_acumulada_mm', 0.0) + chuva_mm
            else:  # Se não achou nenhum válido, começa do zero com a chuva atual
                novo_acumulado = chuva_mm
        else:  # Se o histórico está vazio
            novo_acumulado = chuva_mm

        return {
            "timestamp": timestamp_utc.isoformat(),
            "pluviometria_mm": round(chuva_mm, 2),
            "precipitacao_acumulada_mm": round(novo_acumulado, 2),
            "umidade_1m_perc": round(self.umidade_1m, 2),
            "umidade_2m_perc": round(self.umidade_2m, 2),
            "umidade_3m_perc": round(self.umidade_3m, 2),  # MODIFICADO de 2m5
        }