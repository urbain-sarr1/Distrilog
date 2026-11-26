import json
import os
import threading
import time
import unicodedata
from contextlib import asynccontextmanager

import pika
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import ForeignKey, create_engine, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)


RABBIT_HOST = os.environ.get("RABBIT_HOST", "localhost")

engine = create_engine(
    os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://distrilog:distrilog@localhost:5444/livraison",
    )
)


# ---------- Base de données ----------

class Base(DeclarativeBase):
    pass


class BonLivraison(Base):
    __tablename__ = "bons"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(unique=True)  # pas de doublon de message
    commande_id: Mapped[int] = mapped_column(unique=True)  # un seul bon par commande
    statut: Mapped[str] = mapped_column(default="a_preparer")
    lignes: Mapped[list["LigneBon"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", order_by="LigneBon.id"
    )


class LigneBon(Base):
    __tablename__ = "lignes_bon"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bon_id: Mapped[int] = mapped_column(ForeignKey("bons.id"))
    sku: Mapped[str]
    quantite: Mapped[int]


def verifier_schema(moteur, table: str, colonne: str):
    """Message clair si la base date de la V0 (tables de forme différente)."""
    insp = inspect(moteur)
    if insp.has_table(table) and colonne not in {
        c["name"] for c in insp.get_columns(table)
    }:
        raise RuntimeError(
            f"La table '{table}' date de la V0. Lancez : "
            "docker compose down -v  puis  docker compose up -d --build"
        )


verifier_schema(engine, "bons", "statut")
Base.metadata.create_all(engine)


# ---------- Statuts ----------

# annulee n'est posé que par l'événement commande.annulee, jamais à la main
STATUTS = ("a_preparer", "en_cours", "livree")
TRANSITIONS = {
    "a_preparer": {"en_cours"},
    "en_cours": {"livree"},
    "livree": set(),
    "annulee": set(),
}


def normaliser(valeur: str) -> str:
    """'à préparer' -> 'a_preparer', 'Livrée' -> 'livree'."""
    sans_accents = "".join(
        c for c in unicodedata.normalize("NFKD", valeur) if not unicodedata.combining(c)
    )
    return sans_accents.strip().lower().replace(" ", "_").replace("-", "_")


class StatutIn(BaseModel):
    statut: str

    @field_validator("statut")
    @classmethod
    def valider(cls, v: str) -> str:
        v = normaliser(v)
        if v not in STATUTS:
            raise ValueError(f"Statut inconnu. Valeurs : {', '.join(STATUTS)}")
        return v


def en_dict(b: BonLivraison) -> dict:
    return {
        "id": b.id,
        "commande_id": b.commande_id,
        "statut": b.statut,
        "lignes": [{"sku": l.sku, "quantite": l.quantite} for l in b.lignes],
    }


# ---------- Traitement des événements ----------

def lignes_de(evenement: dict) -> list[dict]:
    # Ancien format (V0) : un seul produit au premier niveau du message
    return evenement.get("lignes") or [
        {"sku": evenement["sku"], "quantite": evenement["quantite"]}
    ]


def creer_bon(evenement: dict, statut: str) -> bool:
    """Crée le bon. Renvoie False si ce message ou cette commande est déjà connu."""
    with Session(engine) as db:
        db.add(
            BonLivraison(
                event_id=evenement["event_id"],
                commande_id=evenement["commande_id"],
                statut=statut,
                lignes=[
                    LigneBon(sku=l["sku"], quantite=l["quantite"])
                    for l in lignes_de(evenement)
                ],
            )
        )
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False


def traiter_commande_passee(evenement: dict):
    if creer_bon(evenement, "a_preparer"):
        print("Bon créé pour la commande", evenement["commande_id"])
    else:
        print("Message déjà traité, on l'ignore")


def traiter_commande_annulee(evenement: dict):
    commande_id = evenement["commande_id"]
    with Session(engine) as db:
        # On n'annule pas un colis déjà livré
        res = db.execute(
            update(BonLivraison)
            .where(
                BonLivraison.commande_id == commande_id,
                BonLivraison.statut.in_(("a_preparer", "en_cours")),
            )
            .values(statut="annulee")
        )
        db.commit()
        if res.rowcount:
            print("Bon annulé pour la commande", commande_id)
            return
        existe = db.scalar(
            select(BonLivraison.id).where(BonLivraison.commande_id == commande_id)
        )
    if existe is None:
        # Annulation arrivée avant la commande : on enregistre un bon déjà annulé,
        # la commande.passee tardive sera ignorée
        creer_bon(evenement, "annulee")
        print("Annulation reçue avant la commande", commande_id)
    else:
        print("Bon déjà livré ou annulé, rien à faire pour", commande_id)


def traiter(corps: bytes, cle: str):
    evenement = json.loads(corps)
    if cle == "commande.annulee":
        traiter_commande_annulee(evenement)
    else:
        traiter_commande_passee(evenement)


# ---------- RabbitMQ ----------

def ecouter():
    connexion = pika.BlockingConnection(pika.ConnectionParameters(RABBIT_HOST))
    canal = connexion.channel()
    canal.exchange_declare(
        exchange="distrilog.events", exchange_type="topic", durable=True
    )
    canal.queue_declare(queue="livraison.commandes", durable=True)
    # Une seule file pour les deux événements : l'ordre est conservé
    for cle in ("commande.passee", "commande.annulee"):
        canal.queue_bind(
            queue="livraison.commandes",
            exchange="distrilog.events",
            routing_key=cle,
        )

    def au_message(ch, method, properties, body):
        try:
            traiter(body, method.routing_key)
        except (ValueError, KeyError, TypeError) as erreur:
            # Message illisible : inutile de le rejouer
            print("Message invalide ignoré :", repr(erreur))
        except Exception as erreur:
            # Ex. base indisponible : on le remet dans la file
            print("Traitement impossible, message remis en file :", erreur)
            time.sleep(2)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return
        ch.basic_ack(delivery_tag=method.delivery_tag)

    canal.basic_consume(queue="livraison.commandes", on_message_callback=au_message)
    canal.start_consuming()


def ecouter_avec_reessai():
    # Si RabbitMQ n'est pas prêt (ou tombe), on réessaie au lieu de tuer le thread
    while True:
        try:
            ecouter()
        except Exception as erreur:
            print("RabbitMQ indisponible, nouvel essai dans 5 s :", erreur)
            time.sleep(5)


@asynccontextmanager
async def demarrage(app):
    threading.Thread(target=ecouter_avec_reessai, daemon=True).start()
    yield


app = FastAPI(title="livraison", lifespan=demarrage)


# ---------- Routes ----------

@app.get("/livraisons")
def lister_livraisons():
    with Session(engine) as db:
        return [
            en_dict(b)
            for b in db.scalars(select(BonLivraison).order_by(BonLivraison.id))
        ]


@app.get("/livraisons/{id}")
def lire_livraison(id: int):
    with Session(engine) as db:
        bon = db.get(BonLivraison, id)
        if bon is None:
            raise HTTPException(status_code=404, detail="Livraison introuvable")
        return en_dict(bon)


@app.put("/livraisons/{id}")
def changer_statut(id: int, data: StatutIn):
    """a_preparer -> en_cours -> livree (un cran à la fois)."""
    with Session(engine) as db:
        bon = db.get(BonLivraison, id)
        if bon is None:
            raise HTTPException(status_code=404, detail="Livraison introuvable")
        ancien = bon.statut
        if data.statut not in TRANSITIONS[ancien]:
            raise HTTPException(
                status_code=409,
                detail=f"Transition interdite : {ancien} -> {data.statut}",
            )
        res = db.execute(
            update(BonLivraison)
            .where(BonLivraison.id == id, BonLivraison.statut == ancien)
            .values(statut=data.statut)
        )
        if res.rowcount == 0:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="La livraison vient de changer, réessayez"
            )
        db.commit()
    with Session(engine) as db:
        return en_dict(db.get(BonLivraison, id))
