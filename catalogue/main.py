import os

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


engine_cat = create_engine(
    os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://distrilog:distrilog@localhost:5441/catalogue",
    )
)
engine_stock = create_engine(
    os.environ.get(
        "STOCK_DATABASE_URL",
        "postgresql+psycopg://distrilog:distrilog@localhost:5442/stock",
    )
)


class Base(DeclarativeBase):
    pass


class Produit(Base):
    __tablename__ = "produits"
    sku: Mapped[str] = mapped_column(primary_key=True)
    nom: Mapped[str]
    prix: Mapped[float]


class Stock(Base):
    __tablename__ = "stock"
    sku: Mapped[str] = mapped_column(primary_key=True)
    quantite: Mapped[int]


Base.metadata.create_all(engine_cat, tables=[Produit.__table__])
Base.metadata.create_all(engine_stock, tables=[Stock.__table__])


app = FastAPI(title="catalogue")


class ProduitIn(BaseModel):
    sku: str
    nom: str
    prix: float
    quantite: int = Field(ge=0)


class ProduitModifIn(BaseModel):
    nom: str
    prix: float = Field(ge=0)


def en_dict(p: Produit) -> dict:
    return {"sku": p.sku, "nom": p.nom, "prix": p.prix}


@app.post("/produits", status_code=201)
def creer_produit(p: ProduitIn):
    # 1. Création du produit dans la base catalogue
    with Session(engine_cat) as db:
        db.add(Produit(sku=p.sku, nom=p.nom, prix=p.prix))
        try:
            db.commit()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="Ce SKU existe déjà")

    # 2. Création de la ligne de stock dans la base stock
    try:
        with Session(engine_stock) as db:
            ligne = db.get(Stock, p.sku)
            if ligne is None:
                db.add(Stock(sku=p.sku, quantite=p.quantite))
            else:
                # Ligne de stock restée d'un ancien produit supprimé
                ligne.quantite = p.quantite
            db.commit()
    except Exception:
        # Compensation : pas de transaction commune entre les deux bases
        with Session(engine_cat) as db:
            produit = db.get(Produit, p.sku)
            if produit:
                db.delete(produit)
                db.commit()
        raise HTTPException(status_code=500, detail="Échec de la création du stock")

    return p


@app.get("/produits")
def lister_produits(
    nom: str | None = Query(default=None, description="Contient (sans tenir compte des majuscules)"),
    prix_min: float | None = Query(default=None, ge=0),
    prix_max: float | None = Query(default=None, ge=0),
):
    requete = select(Produit).order_by(Produit.sku)
    if nom:
        requete = requete.where(Produit.nom.icontains(nom, autoescape=True))
    if prix_min is not None:
        requete = requete.where(Produit.prix >= prix_min)
    if prix_max is not None:
        requete = requete.where(Produit.prix <= prix_max)
    with Session(engine_cat) as db:
        return [en_dict(p) for p in db.scalars(requete)]


@app.get("/produits/{sku}")
def lire_produit(sku: str):
    with Session(engine_cat) as db:
        p = db.get(Produit, sku)
        if p is None:
            raise HTTPException(status_code=404, detail="Produit introuvable")
        return en_dict(p)


@app.put("/produits/{sku}")
def modifier_produit(sku: str, data: ProduitModifIn):
    with Session(engine_cat) as db:
        p = db.get(Produit, sku)
        if p is None:
            raise HTTPException(status_code=404, detail="Produit introuvable")
        p.nom = data.nom
        p.prix = data.prix
        db.commit()
        return en_dict(p)


@app.delete("/produits/{sku}")
def supprimer_produit(sku: str):
    with Session(engine_cat) as db:
        p = db.get(Produit, sku)
        if p is None:
            raise HTTPException(status_code=404, detail="Produit introuvable")
        db.delete(p)
        db.commit()

    # On retire aussi sa ligne de stock (créée avec le produit)
    try:
        with Session(engine_stock) as db:
            ligne = db.get(Stock, sku)
            if ligne is not None:
                db.delete(ligne)
                db.commit()
    except Exception as erreur:
        print("Ligne de stock non supprimée :", sku, erreur)

    return {"sku": sku, "supprime": True}
