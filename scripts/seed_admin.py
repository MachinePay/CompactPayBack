import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.models import Usuario, UserRole
from app.core.security import get_password_hash
from app.db.base import Base

# Use variável de ambiente para senha do admin
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@compactpay.com.br")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

if not ADMIN_PASSWORD:
    raise Exception("Defina a variável de ambiente ADMIN_PASSWORD para criar o admin!")

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

# Cria as tabelas se não existirem
Base.metadata.create_all(bind=engine)

def main():
    db = SessionLocal()
    if db.query(Usuario).filter(Usuario.email == ADMIN_EMAIL).first():
        print("Usuário admin já existe.")
        return
    admin = Usuario(
        email=ADMIN_EMAIL,
        hashed_password=get_password_hash(ADMIN_PASSWORD),
        role=UserRole.admin,
        cliente_id=None
    )
    db.add(admin)
    db.commit()
    print(f"Usuário admin criado: {ADMIN_EMAIL}")
    db.close()

if __name__ == "__main__":
    main()
