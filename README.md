# Simulador de Encosta API

API em Python/FastAPI para simular dados de sensores de chuva e umidade do solo.

## Execução Local

1. Instale as dependências:
   `pip install -r requirements.txt`

2. Rode o servidor:
   `python main.py`

3. Acesse a API:
   `http://127.0.0.1:8000/api/data`

## Deploy no Render

1. Envie este código para um repositório no GitHub.
2. Crie um novo "Web Service" no Render.
3. Conecte seu repositório do GitHub.
4. O Render detectará o Python.
5. Comando de Build: `pip install -r requirements.txt`
6. Comando de Start: `uvicorn main:app --host 0.0.0.0 --port $PORT` (O Render deve pegar isso do `Procfile` automaticamente).