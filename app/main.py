from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import auth, duel, fairness, game, stats, wallet
from app.core.config import settings
from app.core.database import init_db
from app.schemas.game import HealthOut


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Motor de dados com resultados verificaveis (provably fair). "
        "Moedas ficticias: sem depositos, sem levantamentos."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://gamefrontend-pearl.vercel.app",
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(game.router, prefix="/api/v1")
app.include_router(duel.router, prefix="/api/v1")
app.include_router(fairness.router, prefix="/api/v1")
app.include_router(wallet.router, prefix="/api/v1")
app.include_router(stats.router, prefix="/api/v1")


@app.get("/api/v1/health", response_model=HealthOut, tags=["meta"])
def health() -> HealthOut:
    return HealthOut(
        status="ok",
        version="0.1.0",
        house_edge=settings.house_edge,
        min_chance=settings.min_chance,
        max_chance=settings.max_chance,
        min_bet=settings.min_bet,
        max_bet=settings.max_bet,
        duel_max_stake=settings.duel_max_stake,
        duel_target_score=settings.duel_target_score,
    )
