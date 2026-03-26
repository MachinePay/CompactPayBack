# CompactPay Backend

## Descrição

API backend para o sistema CompactPay, desenvolvido com FastAPI, SQLAlchemy, PostgreSQL e MQTT.

## Requisitos

- Python 3.10+
- PostgreSQL
- (Opcional) MQTT Broker

## Instalação

1. Crie e ative um ambiente virtual:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/macOS
   venv\Scripts\activate    # Windows
   ```
2. Instale as dependências:
   ```bash
   pip install -r requirements.txt
   ```
3. Configure o banco de dados PostgreSQL e ajuste as variáveis de ambiente conforme necessário.
4. Execute as migrações Alembic:
   ```bash
   alembic upgrade head
   ```
5. Inicie o servidor:
   ```bash
   uvicorn app.main:app --reload
   ```

## Deploy

- Configure as variáveis de ambiente para produção.
- Adicione as URLs do frontend em `allow_origins` no CORS (main.py).
- Utilize um servidor WSGI/ASGI como Uvicorn ou Gunicorn.

## Contato

Dúvidas ou sugestões: contato@compactpay.com.br
