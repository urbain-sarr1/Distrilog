# Distrilog — V1

**Distrilog** est une application de gestion logistique construite autour de quatre microservices indépendants. La V1 permet de gérer un catalogue de produits (création, modification, suppression, recherche), de consulter et approvisionner les stocks avec une alerte de stock bas, de passer des commandes à plusieurs lignes avec un cycle de statuts et une annulation, puis de suivre la livraison de chaque commande grâce aux événements transmis par RabbitMQ.

Cette version met en œuvre une architecture distribuée avec **FastAPI**, **PostgreSQL**, **RabbitMQ** et **Docker Compose**.

> **Évolutions de la V1 par rapport à la V0 :** commandes multi-produits (panier), statuts de commande et de livraison, annulation de commande avec remise en stock, réapprovisionnement dédié, alerte de stock bas, modification, suppression et recherche de produits. L'ancien format de commande à un seul SKU reste accepté.

---

## Sommaire

- [Architecture](#architecture)
- [Technologies](#technologies)
- [Arborescence](#arborescence)
- [Prérequis](#prérequis)
- [Démarrage avec Docker Compose](#démarrage-avec-docker-compose)
- [Accès aux API](#accès-aux-api)
- [Documentation des endpoints](#documentation-des-endpoints)
- [Scénario de fonctionnement](#scénario-de-fonctionnement)
- [Communication événementielle](#communication-événementielle)
- [Résilience et gestion des erreurs](#résilience-et-gestion-des-erreurs)
- [Persistance des données](#persistance-des-données)
- [Arrêter l'application](#arrêter-lapplication)
- [Limites connues de la V1](#limites-connues-de-la-v1)

---

## Architecture

Distrilog est composé de quatre microservices métier. Chaque service possède sa propre base PostgreSQL :

| Microservice | Responsabilité | Port hôte | Base de données |
|---|---|---:|---|
| **Catalogue** | Créer, consulter, lister, rechercher, modifier et supprimer les produits | `8001` | `catalogue` |
| **Stock** | Consulter les stocks, remplacer ou augmenter une quantité, gérer les réservations et publier l'alerte de stock bas | `8002` | `stock` |
| **Commande** | Créer des commandes à plusieurs lignes, suivre leur statut, les annuler ; réserver et libérer le stock | `8003` | `commande` |
| **Livraison** | Écouter les événements de commande, enregistrer les bons de livraison et suivre leur statut | `8004` | `livraison` |

Les services communiquent de deux manières :

- **HTTP/REST** : le service Commande interroge le Catalogue pour obtenir le prix de chaque produit et appelle le service Stock pour réserver ou libérer les quantités.
- **Événementiel** : le service Commande publie `commande.passee` et `commande.annulee`, le service Stock publie `stock.bas`. Le service Livraison consomme les deux événements de commande pour créer ou annuler un bon de livraison.


## Technologies

- **Python 3.12**
- **FastAPI** et **Uvicorn** : création et exécution des API REST
- **Pydantic** : validation des données entrantes
- **SQLAlchemy** : mapping objet-relationnel et accès aux bases
- **PostgreSQL 16** : persistance des données, avec une base par microservice
- **RabbitMQ** et **Pika** : publication et consommation des événements
- **HTTPX** : appels HTTP entre microservices
- **Tenacity** : nouvelles tentatives sur les erreurs de transport
- **PyBreaker** : disjoncteur protégeant les réservations auprès du service Stock
- **Docker** et **Docker Compose** : conteneurisation et orchestration

---

## Arborescence

```text
Distrilog/
├── catalogue/
│   └── main.py
├── stock/
│   └── main.py
├── commande/
│   └── main.py
├── livraison/
│   └── main.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .dockerignore
├── .gitignore
└── README.md
```

Le Dockerfile est commun aux quatre services. La valeur de l'argument de build `SERVICE` détermine le dossier dont le fichier `main.py` sera copié dans l'image.

---

## Prérequis

Pour lancer le projet avec Docker Compose, il faut disposer de :

- Docker Desktop (ou Docker Engine) démarré ;
- Docker Compose v2, accessible avec la commande `docker compose` ;
- un terminal (CMD, PowerShell ou terminal Linux/macOS).

Aucune installation locale de Python ou de PostgreSQL n'est nécessaire pour le démarrage Docker.

---

## Démarrage avec Docker Compose

Depuis le répertoire racine du projet, exécuter :

```bash
docker compose up -d --build
```

Cette commande construit les images des quatre microservices et démarre également :

- RabbitMQ ;
- PostgreSQL du Catalogue ;
- PostgreSQL du Stock ;
- PostgreSQL des Commandes ;
- PostgreSQL des Livraisons.

> **Passage depuis la V0 :** la structure des tables de Commande et de Livraison a changé (statuts, lignes de commande). Lors de la première utilisation de la V1 sur des volumes créés par la V0, exécuter une fois `docker compose down -v` avant `docker compose up -d --build`. Sans cela, les services Commande et Livraison s'arrêtent au démarrage avec un message qui indique cette commande.

Vérifier l'état des conteneurs :

```bash
docker compose ps
```

Consulter les logs de l'ensemble des services :

```bash
docker compose logs -f
```

Consulter les logs d'un service en particulier :

```bash
docker compose logs -f catalogue
docker compose logs -f stock
docker compose logs -f commande
docker compose logs -f livraison
```

Les bases PostgreSQL et RabbitMQ sont déclarés comme dépendances des services dans Compose. Ils disposent de healthchecks ; les services applicatifs attendent que leur base (et RabbitMQ, pour Stock, Commande et Livraison) soit déclarée saine avant de démarrer.

Le seuil de l'alerte de stock bas se règle avec la variable d'environnement `SEUIL_STOCK_BAS` du service `stock` dans `docker-compose.yml` (5 par défaut).

---

## Accès aux API

| Service | Documentation interactive |
|---|---|
| Catalogue | `http://localhost:8001/docs` |
| Stock | `http://localhost:8002/docs` |
| Commande | `http://localhost:8003/docs` |
| Livraison | `http://localhost:8004/docs` |

Chaque API expose également son schéma OpenAPI via `/openapi.json`. L'interface de gestion de RabbitMQ est disponible sur `http://localhost:15672` (identifiants `guest` / `guest`).

---

## Documentation des endpoints

Les exemples ci-dessous utilisent `curl`. Ils peuvent aussi être exécutés directement depuis la documentation Swagger (`/docs`).

### 1. Catalogue — port 8001

Le Catalogue gère les informations des produits. Lors de la création d'un produit, il enregistre le produit dans sa base puis crée la ligne de stock correspondante dans la base du service Stock.

| Méthode | Route | Description |
|---|---|---|
| `POST` | `/produits` | Créer un produit et initialiser son stock |
| `GET` | `/produits` | Lister les produits, avec recherche et filtres optionnels |
| `GET` | `/produits/{sku}` | Consulter un produit par SKU |
| `PUT` | `/produits/{sku}` | Modifier le nom et le prix d'un produit |
| `DELETE` | `/produits/{sku}` | Supprimer un produit et sa ligne de stock |

**Créer un produit**

```bash
curl -X POST http://localhost:8001/produits \
  -H "Content-Type: application/json" \
  -d '{"sku":"PAL-001","nom":"Palette Europe","prix":12.50,"quantite":30}'
```

Champs attendus :

| Champ | Type | Règle |
|---|---|---|
| `sku` | chaîne | Identifiant du produit, clé primaire |
| `nom` | chaîne | Nom du produit |
| `prix` | nombre | Prix unitaire |
| `quantite` | entier | Quantité initiale, supérieure ou égale à zéro |

Une création avec un SKU déjà utilisé renvoie `409 Conflict`. Si la création de la ligne de stock échoue après l'enregistrement du produit, le service tente de supprimer le produit créé afin de compenser l'échec entre les deux bases.

**Lister et rechercher les produits**

```bash
curl http://localhost:8001/produits
curl "http://localhost:8001/produits?nom=palette&prix_max=20"
```

Paramètres optionnels, combinables :

| Paramètre | Effet |
|---|---|
| `nom` | Le nom contient la valeur, sans tenir compte des majuscules |
| `prix_min` | Prix supérieur ou égal à la valeur |
| `prix_max` | Prix inférieur ou égal à la valeur |

Les résultats sont triés par SKU. Sans paramètre, tous les produits sont renvoyés.

**Consulter un produit**

```bash
curl http://localhost:8001/produits/PAL-001
```

Un SKU inconnu renvoie `404 Not Found`.

**Modifier un produit**

```bash
curl -X PUT http://localhost:8001/produits/PAL-001 \
  -H "Content-Type: application/json" \
  -d '{"nom":"Palette Europe 80x120","prix":13.00}'
```

`nom` et `prix` sont obligatoires, le prix doit être supérieur ou égal à zéro. Le SKU et le stock ne changent pas. Un SKU inconnu renvoie `404`.

**Supprimer un produit**

```bash
curl -X DELETE http://localhost:8001/produits/PAL-001
```

La réponse est `{"sku":"PAL-001","supprime":true}`. La ligne de stock du produit est supprimée aussi. Les commandes déjà passées conservent leur historique. Un SKU inconnu renvoie `404`.

### 2. Stock — port 8002

Le service Stock gère la quantité disponible pour chaque SKU.

| Méthode | Route | Description |
|---|---|---|
| `GET` | `/stocks` | Lister les stocks |
| `GET` | `/stocks/{sku}` | Consulter le stock d'un SKU |
| `PUT` | `/stocks/{sku}` | Remplacer la quantité en stock |
| `POST` | `/stocks/{sku}/ajouter` | Réapprovisionner : ajouter une quantité à celle existante |

**Consulter tous les stocks**

```bash
curl http://localhost:8002/stocks
```

**Consulter le stock d'un produit**

```bash
curl http://localhost:8002/stocks/PAL-001
```

**Remplacer la quantité**

```bash
curl -X PUT http://localhost:8002/stocks/PAL-001 \
  -H "Content-Type: application/json" \
  -d '{"quantite":45}'
```

La quantité doit être un entier supérieur ou égal à zéro. Un SKU absent renvoie `404 Not Found`.

**Réapprovisionner**

```bash
curl -X POST http://localhost:8002/stocks/PAL-001/ajouter \
  -H "Content-Type: application/json" \
  -d '{"quantite":10}'
```

La quantité est ajoutée à celle qui existe déjà (45 devient 55), alors que le `PUT` la remplace. La quantité doit être strictement supérieure à zéro. Un SKU absent renvoie `404`.

Le service possède également des routes internes utilisées par le service Commande pour les mouvements de stock :

| Méthode | Route | Description |
|---|---|---|
| `POST` | `/stocks/{sku}/reserver` | Décrémenter le stock d'un SKU si la quantité est disponible |
| `POST` | `/stocks/{sku}/liberer` | Réajouter une quantité au stock d'un SKU |
| `POST` | `/reservations` | Réserver un panier entier : tout est réservé, ou rien ne bouge |
| `POST` | `/reservations/liberer` | Remettre en stock un panier entier |

Ces routes sont déclarées avec `include_in_schema=False` : elles ne figurent donc pas dans la documentation OpenAPI publique.

**Alerte de stock bas :** quand la quantité restante d'un produit est inférieure ou égale au seuil (5 par défaut) après une réservation ou un `PUT`, le service publie l'événement `stock.bas` et écrit une ligne dans ses logs (`docker compose logs stock`).

### 3. Commande — port 8003

Le service Commande crée des commandes portant sur un ou plusieurs produits. Avant de l'enregistrer, il récupère le prix de chaque produit dans le Catalogue, puis réserve l'ensemble des quantités auprès du service Stock en une seule opération.

| Méthode | Route | Description |
|---|---|---|
| `POST` | `/commandes` | Créer une commande (panier) et réserver le stock |
| `GET` | `/commandes` | Lister les commandes |
| `GET` | `/commandes/{id}` | Consulter une commande par identifiant |
| `PUT` | `/commandes/{id}/statut` | Changer le statut d'une commande |
| `DELETE` | `/commandes/{id}` | Annuler une commande et remettre le stock |

**Créer une commande**

```bash
curl -X POST http://localhost:8003/commandes \
  -H "Content-Type: application/json" \
  -d '{"lignes":[{"sku":"PAL-001","quantite":3},{"sku":"BAL-001","quantite":2}]}'
```

Le corps de la requête contient :

| Champ | Type | Règle |
|---|---|---|
| `lignes` | liste | De 1 à 50 lignes, chacune avec `sku` et `quantite` |
| `lignes[].sku` | chaîne | SKU du produit à commander |
| `lignes[].quantite` | entier | Strictement supérieur à zéro |

Les lignes d'un même SKU sont additionnées. L'ancien format à un seul produit reste accepté, mais il ne peut pas être combiné avec `lignes` :

```bash
curl -X POST http://localhost:8003/commandes \
  -H "Content-Type: application/json" \
  -d '{"sku":"PAL-001","quantite":3}'
```

Le total est la somme des lignes, calculée à partir des prix du Catalogue : `prix unitaire × quantité` pour chaque ligne. La commande est créée au statut `en_attente`. Exemple de réponse :

```json
{
  "id": 1,
  "statut": "en_attente",
  "total": 53.5,
  "lignes": [
    {"sku": "BAL-001", "quantite": 2, "prix_unitaire": 8.0},
    {"sku": "PAL-001", "quantite": 3, "prix_unitaire": 12.5}
  ]
}
```

**Lister les commandes**

```bash
curl http://localhost:8003/commandes
```

**Consulter une commande**

```bash
curl http://localhost:8003/commandes/1
```

Une commande inexistante renvoie `404 Not Found`.

**Statuts de commande**

| Statut | Signification | Transitions autorisées |
|---|---|---|
| `en_attente` | Commande enregistrée, stock réservé | `confirmee`, `annulee` |
| `confirmee` | Commande confirmée | `expediee`, `annulee` |
| `expediee` | Commande expédiée | aucune |
| `annulee` | Commande annulée, stock remis | aucune |

```bash
curl -X PUT http://localhost:8003/commandes/1/statut \
  -H "Content-Type: application/json" \
  -d '{"statut":"confirmee"}'
```

Les accents sont acceptés en entrée (`confirmée`, `expédiée`, `annulée`), les réponses utilisent les valeurs sans accent. Une transition interdite renvoie `409`, un statut inconnu renvoie `422`. Demander le statut `annulee` déclenche la même annulation que la route `DELETE`.

**Annuler une commande**

```bash
curl -X DELETE http://localhost:8003/commandes/1
```

L'annulation remet les quantités en stock, passe la commande au statut `annulee` et publie l'événement `commande.annulee`. La commande reste consultable. Elle est impossible si la commande est déjà expédiée ou déjà annulée (`409`). Si le service Stock est indisponible, la réponse est `503` et la commande garde son statut précédent : l'annulation peut être relancée plus tard.

Erreurs principales lors de la création :

- `404` : produit ou stock introuvable (le SKU concerné est indiqué) ;
- `409` : stock insuffisant (le SKU concerné est indiqué) ;
- `422` : corps de requête invalide (aucune ligne, quantité nulle, formats mélangés) ;
- `503` : Catalogue ou Stock indisponible ;
- `502` : réponse inattendue d'un service appelé ;
- `500` : échec de l'enregistrement de la commande.

### 4. Livraison — port 8004

Le service Livraison consomme les événements `commande.passee` et `commande.annulee`. Il crée un bon de livraison par commande, annule le bon quand la commande est annulée, et permet de suivre l'avancement de la livraison.

| Méthode | Route | Description |
|---|---|---|
| `GET` | `/livraisons` | Lister les bons de livraison |
| `GET` | `/livraisons/{id}` | Consulter un bon par identifiant |
| `PUT` | `/livraisons/{id}` | Changer le statut d'un bon |

**Lister les livraisons**

```bash
curl http://localhost:8004/livraisons
```

Un bon contient son identifiant, l'identifiant de commande, le statut et les lignes (SKU et quantité) :

```json
{
  "id": 1,
  "commande_id": 1,
  "statut": "a_preparer",
  "lignes": [
    {"sku": "BAL-001", "quantite": 2},
    {"sku": "PAL-001", "quantite": 3}
  ]
}
```

**Statuts de livraison**

| Statut | Signification | Transitions autorisées |
|---|---|---|
| `a_preparer` | Bon créé à la réception de la commande | `en_cours` |
| `en_cours` | Livraison en cours | `livree` |
| `livree` | Livraison terminée | aucune |
| `annulee` | Posé automatiquement quand la commande est annulée | aucune |

```bash
curl -X PUT http://localhost:8004/livraisons/1 \
  -H "Content-Type: application/json" \
  -d '{"statut":"en_cours"}'
```

Le statut avance d'un cran à la fois : passer directement de `a_preparer` à `livree` renvoie `409`. Les accents et les espaces sont acceptés (`à préparer`, `en cours`, `livrée`). Le statut `annulee` ne peut pas être demandé à la main (`422`) et un bon annulé ne peut plus changer (`409`). Un bon inconnu renvoie `404`.

---

## Scénario de fonctionnement

Le scénario suivant permet de vérifier le parcours de bout en bout.

### Étape 1 — Créer deux produits

```bash
curl -X POST http://localhost:8001/produits \
  -H "Content-Type: application/json" \
  -d '{"sku":"PAL-001","nom":"Palette Europe","prix":12.50,"quantite":20}'

curl -X POST http://localhost:8001/produits \
  -H "Content-Type: application/json" \
  -d '{"sku":"BAL-001","nom":"Ballon rouge","prix":8.00,"quantite":20}'
```

Les produits sont enregistrés dans le Catalogue et leurs stocks initiaux sont créés dans le service Stock.

### Étape 2 — Rechercher, modifier

```bash
curl "http://localhost:8001/produits?nom=palette&prix_max=20"
```

La réponse contient uniquement `PAL-001`. Le produit peut ensuite être modifié avec `PUT /produits/PAL-001`.

### Étape 3 — Réapprovisionner

```bash
curl -X POST http://localhost:8002/stocks/PAL-001/ajouter \
  -H "Content-Type: application/json" \
  -d '{"quantite":10}'
```

Le stock de `PAL-001` passe de `20` à `30`, alors qu'un `PUT` aurait remplacé la quantité.

### Étape 4 — Passer une commande à plusieurs lignes

```bash
curl -X POST http://localhost:8003/commandes \
  -H "Content-Type: application/json" \
  -d '{"lignes":[{"sku":"PAL-001","quantite":3},{"sku":"BAL-001","quantite":2}]}'
```

Le service Commande récupère les prix, réserve les deux lignes en une seule opération, enregistre la commande au statut `en_attente` et publie l'événement `commande.passee`. Le total attendu est `53.5` (3 × 12,50 + 2 × 8,00).

### Étape 5 — Vérifier les quantités restantes

```bash
curl http://localhost:8002/stocks
```

Les quantités attendues sont `27` pour `PAL-001` et `18` pour `BAL-001`.

### Étape 6 — Vérifier le bon de livraison

```bash
curl http://localhost:8004/livraisons
```

Après consommation de l'événement par le service Livraison, un bon au statut `a_preparer` correspondant à la commande doit apparaître. La consommation étant asynchrone, il peut être nécessaire de relancer la consultation après un court instant.

### Étape 7 — Faire avancer la commande et la livraison

```bash
curl -X PUT http://localhost:8003/commandes/1/statut \
  -H "Content-Type: application/json" \
  -d '{"statut":"confirmee"}'

curl -X PUT http://localhost:8004/livraisons/1 \
  -H "Content-Type: application/json" \
  -d '{"statut":"en_cours"}'
```

La commande passe à `confirmee` et le bon à `en_cours`.

### Étape 8 — Annuler la commande

```bash
curl -X DELETE http://localhost:8003/commandes/1
```

La commande passe à `annulee` et les quantités reviennent en stock (`30` et `20`). Après un court instant, le bon de livraison passe lui aussi à `annulee`. Une nouvelle annulation de la même commande renvoie `409`.

### Étape 9 — Déclencher l'alerte de stock bas

```bash
curl -X PUT http://localhost:8002/stocks/PAL-001 \
  -H "Content-Type: application/json" \
  -d '{"quantite":4}'

docker compose logs stock
```

Les logs du service Stock contiennent `Stock bas : PAL-001 = 4 (seuil 5)` et l'événement `stock.bas` est publié sur RabbitMQ.

---

## Communication événementielle

Les services publient leurs événements sur l'exchange RabbitMQ suivant :

- **Exchange :** `distrilog.events`
- **Type :** `topic`
- **Durable :** oui

| Routing key | Publié par | Consommé par | Déclencheur |
|---|---|---|---|
| `commande.passee` | Commande | Livraison | Création d'une commande |
| `commande.annulee` | Commande | Livraison | Annulation d'une commande |
| `stock.bas` | Stock | aucun consommateur dans la V1 | Quantité restante inférieure ou égale au seuil |

Messages publiés :

```json
{
  "event_id": "identifiant-unique-genere-par-le-service",
  "commande_id": 1,
  "lignes": [
    {"sku": "PAL-001", "quantite": 3},
    {"sku": "BAL-001", "quantite": 2}
  ]
}
```

Ce format est commun à `commande.passee` et `commande.annulee`. L'événement `stock.bas` contient :

```json
{
  "event_id": "identifiant-unique-genere-par-le-service",
  "sku": "PAL-001",
  "quantite": 4,
  "seuil": 5
}
```

Le service Livraison déclare et écoute la file durable `livraison.commandes`, liée à l'exchange avec les routing keys `commande.passee` et `commande.annulee`. Une seule file est utilisée pour que l'ordre des messages soit conservé.

Règles du consommateur :

- `event_id` et `commande_id` sont uniques dans la table des bons : un message déjà traité est reconnu et ignoré, sans doublon ;
- `commande.annulee` passe le bon à `annulee` s'il est à `a_preparer` ou `en_cours` ; un bon déjà `livree` n'est pas modifié ;
- si une annulation arrive avant la commande, un bon déjà annulé est enregistré et la commande tardive est ignorée ;
- l'ancien format de message de la V0 (un seul `sku` et `quantite` au premier niveau) est toujours lu ;
- un message illisible est journalisé et acquitté, il n'est pas rejoué ; si le traitement échoue pour une raison temporaire (base indisponible), le message est remis dans la file ;
- après traitement, le consommateur acquitte le message auprès de RabbitMQ.

Si RabbitMQ est indisponible au démarrage ou pendant l'écoute, le consommateur réessaie de se connecter avec une attente de cinq secondes entre les tentatives.

Les événements sont publiés après l'envoi de la réponse HTTP, avec un délai de connexion de deux secondes : une panne de RabbitMQ ne ralentit ni ne bloque une commande ou une réservation.

---

## Résilience et gestion des erreurs

Le service Commande intègre plusieurs mécanismes de résilience pour ses appels HTTP :

- **Timeout HTTP :** deux secondes par appel ;
- **Retry :** jusqu'à trois tentatives avec attente exponentielle. Les lectures (prix au Catalogue) sont rejouées sur toute erreur de transport. Les appels qui modifient le stock (réservation, libération) ne sont rejoués que si la requête n'a pas pu partir, afin de ne jamais réserver ou libérer deux fois ;
- **Circuit breaker Stock :** après trois échecs, les réservations auprès du service Stock sont interrompues pendant dix secondes et la réponse est immédiate (`503`) ;
- **Réservation tout ou rien :** les lignes d'un panier sont réservées dans une seule transaction côté Stock. Si une ligne manque de stock, aucune quantité n'est retirée ;
- **Compensation :** si l'enregistrement local de la commande échoue après la réservation, le service tente de libérer les quantités réservées ;
- **Annulation sûre :** le statut `annulee` est posé par une mise à jour conditionnelle, ce qui empêche deux annulations simultanées de remettre le stock deux fois. Si le Stock ne répond pas, le statut précédent est rétabli ;
- **Publication non bloquante :** une indisponibilité de RabbitMQ est journalisée et ne remet pas en cause la commande ou le stock déjà enregistrés.

La réservation du stock est réalisée par une mise à jour conditionnelle en base : la quantité n'est décrémentée que si le stock disponible est supérieur ou égal à la quantité demandée. Les statuts de commande et de livraison sont eux aussi modifiés par mise à jour conditionnelle, pour éviter les changements concurrents contradictoires.

Au démarrage, les services Commande et Livraison vérifient la structure de leurs tables. Si elles datent de la V0, le service s'arrête avec le message `docker compose down -v puis docker compose up -d --build`.

> La compensation est une opération distribuée best-effort : si le service Stock est inaccessible au moment de la libération, le code journalise l'échec. Il ne s'agit pas d'une transaction distribuée garantie entre les bases.

---

## Persistance des données

Docker Compose déclare quatre volumes nommés :

| Volume | Données conservées |
|---|---|
| `catalogue_data` | Produits |
| `stock_data` | Quantités en stock |
| `commande_data` | Commandes et lignes de commande |
| `livraison_data` | Bons de livraison, lignes et identifiants d'événements traités |

Les données sont conservées lorsque les conteneurs sont arrêtés puis redémarrés avec `docker compose down`. RabbitMQ n'a pas de volume : ses files sont recréées au démarrage.

Pour supprimer également les volumes et donc réinitialiser les données, utiliser :

```bash
docker compose down -v
```

**Attention :** cette commande supprime définitivement les données persistées dans les volumes du projet.

---

## Arrêter l'application

Arrêter et supprimer les conteneurs et le réseau Compose, tout en conservant les volumes :

```bash
docker compose down
```

Relancer ultérieurement :

```bash
docker compose up -d
```


