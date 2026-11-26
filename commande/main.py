import json
import os
import unicodedata
import uuid

import httpx
import pika
import pybreaker
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import ForeignKey, create_engine, inspect, select, update
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


CATALOGUE_URL = os.environ.get("CATALOGUE_URL", "http://localhost:8000")
STOCK_URL = os.environ.get("STOCK_URL", "http://localhost:8001")
RABBIT_HOST = os.environ.get("RABBIT_HOST", "localhost")

engine = create_engine(
    os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://distrilog:distrilog@localhost:5443/commande",
    )
)


# ---------- Base de données ----------

class Base(DeclarativeBase):
    pass


class Commande(Base):
    __tablename__ = "commandes"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statut: Mapped[str] = mapped_column(default="en_attente")
    total: Mapped[float]
    lignes: Mapped[list["LigneCommande"]] = relationship(
        lazy="selectin", cascade="all, delete-orphan", order_by="LigneCommande.id"
    )


class LigneCommande(Base):
    __tablename__ = "lignes_commande"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    commande_id: Mapped[int] = mapped_column(ForeignKey("commandes.id"))
    sku: Mapped[str]
    quantite: Mapped[int]
    prix_unitaire: Mapped[float]


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


verifier_schema(engine, "commandes", "statut")
Base.metadata.create_all(engine)


# ---------- Statuts ----------

STATUTS = ("en_attente", "confirmee", "expediee", "annulee")
TRANSITIONS = {
    "en_attente": {"confirmee", "annulee"},
    "confirmee": {"expediee", "annulee"},
    "expediee": set(),
    "annulee": set(),
}


def normaliser(valeur: str) -> str:
    """'Confirmée' -> 'confirmee', 'en attente' -> 'en_attente'."""
    sans_accents = "".join(
        c for c in unicodedata.normalize("NFKD", valeur) if not unicodedata.combining(c)
    )
    return sans_accents.strip().lower().replace(" ", "_").replace("-", "_")


# ---------- Contrats d'entrée ----------

class LigneIn(BaseModel):
    sku: str
    quantite: int = Field(gt=0)


class CommandeIn(BaseModel):
    # Panier : {"lignes": [{"sku": ..., "quantite": ...}, ...]}
    # Ancien format (V0) toujours accepté : {"sku": ..., "quantite": ...}
    lignes: list[LigneIn] | None = Field(default=None, max_length=50)
    sku: str | None = None
    quantite: int | None = Field(default=None, gt=0)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"lignes": [{"sku": "PAL-01", "quantite": 2}, {"sku": "AU0002", "quantite": 1}]}
            ]
        }
    }

    @model_validator(mode="after")
    def verifier(self):
        if self.lignes and (self.sku or self.quantite):
            raise ValueError("Utilisez soit 'lignes', soit 'sku' + 'quantite'")
        if not self.lignes and not (self.sku and self.quantite):
            raise ValueError("Fournir 'lignes', ou 'sku' + 'quantite'")
        return self

    def panier(self) -> dict[str, int]:
        """SKU -> quantité, doublons additionnés, ordre stable."""
        lignes = self.lignes or [LigneIn(sku=self.sku, quantite=self.quantite)]
        panier: dict[str, int] = {}
        for ligne in lignes:
            panier[ligne.sku] = panier.get(ligne.sku, 0) + ligne.quantite
        return dict(sorted(panier.items()))


class StatutIn(BaseModel):
    statut: str

    @field_validator("statut")
    @classmethod
    def valider(cls, v: str) -> str:
        v = normaliser(v)
        if v not in STATUTS:
            raise ValueError(f"Statut inconnu. Valeurs : {', '.join(STATUTS)}")
        return v


app = FastAPI(title="commande")


def en_dict(c: Commande) -> dict:
    return {
        "id": c.id,
        "statut": c.statut,
        "total": c.total,
        "lignes": [
            {"sku": l.sku, "quantite": l.quantite, "prix_unitaire": l.prix_unitaire}
            for l in c.lignes
        ],
    }


# ---------- Résilience : timeout, retry, disjoncteur ----------

# Après 3 échecs, on arrête d'appeler Stock pendant 10 secondes
disjoncteur_stock = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=10)


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.2),
    reraise=True,
)
def appeler(methode: str, url: str, **kwargs):
    """Un appel HTTP (lecture) avec timeout (2 s) et 3 essais."""
    return httpx.request(methode, url, timeout=2.0, **kwargs)


@retry(
    retry=retry_if_exception_type((httpx.ConnectError, httpx.ConnectTimeout)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.2),
    reraise=True,
)
def appeler_mouvement(methode: str, url: str, **kwargs):
    """Appel qui modifie le stock : on ne rejoue que si la requête n'est pas partie,
    pour ne jamais réserver ou libérer deux fois."""
    return httpx.request(methode, url, timeout=2.0, **kwargs)


def corps_lot(panier: dict[str, int]) -> dict:
    return {"lignes": [{"sku": s, "quantite": q} for s, q in panier.items()]}


def reserver_stock(panier: dict[str, int]):
    # La réservation passe par le disjoncteur
    return disjoncteur_stock.call(
        appeler_mouvement, "POST", f"{STOCK_URL}/reservations", json=corps_lot(panier)
    )


def liberer_stock(panier: dict[str, int]) -> bool:
    """Compensation : hors disjoncteur. Renvoie False si Stock n'a pas confirmé."""
    try:
        r = appeler_mouvement(
            "POST", f"{STOCK_URL}/reservations/liberer", json=corps_lot(panier)
        )
    except httpx.TransportError as erreur:
        print("Libération impossible, réservation orpheline :", panier, erreur)
        return False
    if r.status_code != 200:
        print("Libération refusée par Stock :", panier, r.status_code)
        return False
    return True


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
        # RabbitMQ en panne : la commande reste valide
        print(f"Événement {cle} non publié :", erreur)


def evenement_commande(commande_id: int, panier: dict[str, int]) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "commande_id": commande_id,
        "lignes": [{"sku": s, "quantite": q} for s, q in panier.items()],
    }


# ---------- Routes ----------

@app.post("/commandes", status_code=201)
def creer_commande(data: CommandeIn, taches: BackgroundTasks):
    panier = data.panier()

    # 1. Prix de chaque produit, depuis le catalogue
    prix: dict[str, float] = {}
    for sku in panier:
        try:
            r = appeler("GET", f"{CATALOGUE_URL}/produits/{sku}")
        except httpx.TransportError:
            raise HTTPException(
                status_code=503, detail="Catalogue indisponible, réessayez plus tard"
            )
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Produit introuvable : {sku}")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="Erreur du service catalogue")
        prix[sku] = r.json()["prix"]
    total = round(sum(prix[s] * q for s, q in panier.items()), 2)

    # 2. Réservation atomique de tout le panier, protégée par le disjoncteur
    try:
        r = reserver_stock(panier)
    except (httpx.TransportError, pybreaker.CircuitBreakerError):
        # Mode dégradé : on répond proprement au lieu de tout bloquer
        raise HTTPException(
            status_code=503, detail="Stock indisponible, commande non prise"
        )
    if r.status_code in (404, 409):
        raise HTTPException(status_code=r.status_code, detail=r.json()["detail"])
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Erreur du service stock")

    # 3. Création de la commande, avec compensation si elle échoue
    try:
        with Session(engine) as db:
            commande = Commande(
                statut="en_attente",
                total=total,
                lignes=[
                    LigneCommande(sku=s, quantite=q, prix_unitaire=prix[s])
                    for s, q in panier.items()
                ],
            )
            db.add(commande)
            db.commit()
            db.refresh(commande)
            resultat = en_dict(commande)
    except Exception:
        liberer_stock(panier)
        raise HTTPException(status_code=500, detail="Échec de la création de la commande")

    # 4. On PRÉVIENT les autres services (après la réponse), sans les attendre
    taches.add_task(
        publier_evenement, "commande.passee", evenement_commande(resultat["id"], panier)
    )
    return resultat


@app.get("/commandes")
def lister_commandes():
    with Session(engine) as db:
        return [en_dict(c) for c in db.scalars(select(Commande).order_by(Commande.id))]


@app.get("/commandes/{id}")
def lire_commande(id: int):
    with Session(engine) as db:
        commande = db.get(Commande, id)
        if commande is None:
            raise HTTPException(status_code=404, detail="Commande introuvable")
        return en_dict(commande)


def annuler(id: int, taches: BackgroundTasks) -> dict:
    with Session(engine) as db:
        commande = db.get(Commande, id)
        if commande is None:
            raise HTTPException(status_code=404, detail="Commande introuvable")
        ancien = commande.statut
        panier = {l.sku: l.quantite for l in commande.lignes}
        if ancien == "annulee":
            raise HTTPException(status_code=409, detail="Commande déjà annulée")
        if ancien == "expediee":
            raise HTTPException(
                status_code=409, detail="Commande expédiée : annulation impossible"
            )
        # On « prend » l'annulation d'un coup : deux annulations simultanées
        # ne peuvent pas libérer le stock deux fois
        res = db.execute(
            update(Commande)
            .where(Commande.id == id, Commande.statut == ancien)
            .values(statut="annulee")
        )
        if res.rowcount == 0:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="La commande vient de changer, réessayez"
            )
        db.commit()

    # Remise en stock ; si Stock ne répond pas, on remet l'ancien statut
    if not liberer_stock(panier):
        with Session(engine) as db:
            db.execute(
                update(Commande)
                .where(Commande.id == id, Commande.statut == "annulee")
                .values(statut=ancien)
            )
            db.commit()
        raise HTTPException(
            status_code=503,
            detail="Stock indisponible, annulation non effectuée, réessayez plus tard",
        )

    taches.add_task(
        publier_evenement, "commande.annulee", evenement_commande(id, panier)
    )
    with Session(engine) as db:
        return en_dict(db.get(Commande, id))


@app.delete("/commandes/{id}")
def annuler_commande(id: int, taches: BackgroundTasks):
    """Annule la commande : le stock est remis, l'événement commande.annulee part."""
    return annuler(id, taches)


@app.put("/commandes/{id}/statut")
def changer_statut(id: int, data: StatutIn, taches: BackgroundTasks):
    """en_attente -> confirmee -> expediee ; annulee tant que non expédiée."""
    if data.statut == "annulee":
        return annuler(id, taches)

    with Session(engine) as db:
        commande = db.get(Commande, id)
        if commande is None:
            raise HTTPException(status_code=404, detail="Commande introuvable")
        ancien = commande.statut
        if data.statut not in TRANSITIONS[ancien]:
            raise HTTPException(
                status_code=409,
                detail=f"Transition interdite : {ancien} -> {data.statut}",
            )
        res = db.execute(
            update(Commande)
            .where(Commande.id == id, Commande.statut == ancien)
            .values(statut=data.statut)
        )
        if res.rowcount == 0:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="La commande vient de changer, réessayez"
            )
        db.commit()
    with Session(engine) as db:
        return en_dict(db.get(Commande, id))
