import json
import os
import uuid

import pika
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


RABBIT_HOST = os.environ.get("RABBIT_HOST", "localhost")
# Alerte « stock bas » : quantité restante <= seuil
SEUIL_STOCK_BAS = int(os.environ.get("SEUIL_STOCK_BAS", "5"))

engine = create_engine(
    os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://distrilog:distrilog@localhost:5442/stock",
    )
)


class Base(DeclarativeBase):
    pass


class Stock(Base):
    __tablename__ = "stock"
    sku: Mapped[str] = mapped_column(primary_key=True)
    quantite: Mapped[int]


Base.metadata.create_all(engine)


app = FastAPI(title="stock")


class QuantiteIn(BaseModel):
    quantite: int = Field(ge=0)


class MouvementIn(BaseModel):
    quantite: int = Field(gt=0)


class LigneStock(BaseModel):
    sku: str
    quantite: int = Field(gt=0)


class LotIn(BaseModel):
    lignes: list[LigneStock] = Field(min_length=1)


def en_dict(stock: Stock) -> dict:
    return {"sku": stock.sku, "quantite": stock.quantite}


def fusionner(lignes: list[LigneStock]) -> dict[str, int]:
    """Additionne les doublons de SKU et trie : ordre de verrouillage stable."""
    total: dict[str, int] = {}
    for ligne in lignes:
        total[ligne.sku] = total.get(ligne.sku, 0) + ligne.quantite
    return dict(sorted(total.items()))


# ---------- RabbitMQ ----------

def publier_evenement(cle: str, evenement: dict):
    try:
        connexion = pika.BlockingConnection(
            pika.ConnectionParameters(
                RABBIT_HOST, socket_timeout=2, connection_attempts=1
            )
        )
        canal = connexion.channel()
        canal.exchange_declare(
            exchange="distrilog.events", exchange_type="topic", durable=True
        )
        canal.basic_publish(
            exchange="distrilog.events",
            routing_key=cle,
            body=json.dumps(evenement),
        )
        connexion.close()
    except Exception as erreur:
        # RabbitMQ en panne : le stock reste valide
        print(f"Événement {cle} non publié :", erreur)


def alerter_si_bas(taches: BackgroundTasks, sku: str, quantite: int):
    """Publie stock.bas (après la réponse) si la quantité passe sous le seuil."""
    if quantite <= SEUIL_STOCK_BAS:
        print(f"Stock bas : {sku} = {quantite} (seuil {SEUIL_STOCK_BAS})")
        taches.add_task(
            publier_evenement,
            "stock.bas",
            {
                "event_id": str(uuid.uuid4()),
                "sku": sku,
                "quantite": quantite,
                "seuil": SEUIL_STOCK_BAS,
            },
        )


# ---------- Routes publiques (visibles dans /docs) ----------

@app.put("/stocks/{sku}")
def modifier_quantite(sku: str, data: QuantiteIn, taches: BackgroundTasks):
    with Session(engine) as db:
        stock = db.get(Stock, sku)
        if stock is None:
            raise HTTPException(status_code=404, detail="Stock introuvable")
        stock.quantite = data.quantite
        db.commit()
        resultat = en_dict(stock)
    alerter_si_bas(taches, sku, resultat["quantite"])
    return resultat


@app.post("/stocks/{sku}/ajouter")
def ajouter_quantite(sku: str, data: MouvementIn):
    """Réapprovisionnement : ajoute à la quantité existante (le PUT, lui, remplace)."""
    with Session(engine) as db:
        res = db.execute(
            update(Stock)
            .where(Stock.sku == sku)
            .values(quantite=Stock.quantite + data.quantite)
        )
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail="Stock introuvable")
        db.commit()
        return en_dict(db.get(Stock, sku))


@app.get("/stocks")
def lister_stocks():
    with Session(engine) as db:
        return [en_dict(stock) for stock in db.scalars(select(Stock))]


@app.get("/stocks/{sku}")
def lire_stock(sku: str):
    with Session(engine) as db:
        stock = db.get(Stock, sku)
        if stock is None:
            raise HTTPException(status_code=404, detail="Stock introuvable")
        return en_dict(stock)


# ---------- Routes internes (cachées de /docs) ----------

@app.post("/stocks/{sku}/reserver", include_in_schema=False)
def reserver(sku: str, data: MouvementIn, taches: BackgroundTasks):
    with Session(engine) as db:
        res = db.execute(
            update(Stock)
            .where(Stock.sku == sku, Stock.quantite >= data.quantite)
            .values(quantite=Stock.quantite - data.quantite)
        )
        if res.rowcount == 0:
            if db.get(Stock, sku) is None:
                raise HTTPException(status_code=404, detail="Stock introuvable")
            raise HTTPException(status_code=409, detail="Stock insuffisant")
        db.commit()
        resultat = en_dict(db.get(Stock, sku))
    alerter_si_bas(taches, sku, resultat["quantite"])
    return resultat


@app.post("/stocks/{sku}/liberer", include_in_schema=False)
def liberer(sku: str, data: MouvementIn):
    with Session(engine) as db:
        res = db.execute(
            update(Stock)
            .where(Stock.sku == sku)
            .values(quantite=Stock.quantite + data.quantite)
        )
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail="Stock introuvable")
        db.commit()
        return en_dict(db.get(Stock, sku))


# Réservation d'un panier entier : tout passe, ou rien ne bouge (une seule transaction)
@app.post("/reservations", include_in_schema=False)
def reserver_lot(lot: LotIn, taches: BackgroundTasks):
    lignes = fusionner(lot.lignes)
    with Session(engine) as db:
        for sku, quantite in lignes.items():
            res = db.execute(
                update(Stock)
                .where(Stock.sku == sku, Stock.quantite >= quantite)
                .values(quantite=Stock.quantite - quantite)
            )
            if res.rowcount == 0:
                db.rollback()
                if db.get(Stock, sku) is None:
                    raise HTTPException(
                        status_code=404, detail=f"Stock introuvable : {sku}"
                    )
                raise HTTPException(
                    status_code=409, detail=f"Stock insuffisant : {sku}"
                )
        db.commit()
        restant = [en_dict(db.get(Stock, sku)) for sku in lignes]
    for ligne in restant:
        alerter_si_bas(taches, ligne["sku"], ligne["quantite"])
    return {"lignes": restant}


@app.post("/reservations/liberer", include_in_schema=False)
def liberer_lot(lot: LotIn):
    lignes = fusionner(lot.lignes)
    introuvables = []
    with Session(engine) as db:
        for sku, quantite in lignes.items():
            res = db.execute(
                update(Stock)
                .where(Stock.sku == sku)
                .values(quantite=Stock.quantite + quantite)
            )
            if res.rowcount == 0:
                # Produit supprimé entre-temps : rien à remettre en stock
                introuvables.append(sku)
        db.commit()
    return {"liberees": [s for s in lignes if s not in introuvables], "introuvables": introuvables}
