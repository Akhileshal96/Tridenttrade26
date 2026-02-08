# Deployment guide

## 1) Build image

```bash
docker build -t tridenttrade-bot -f deployment/Dockerfile .
```

## 2) Configure secrets

Create an env file from `.env.example` and populate valid Kite credentials.

## 3) Run

Research only:
```bash
docker run --rm --env-file .env tridenttrade-bot research
```

Trading only:
```bash
docker run --rm --env-file .env tridenttrade-bot trade
```

Research + trade sequence:
```bash
docker run --rm --env-file .env tridenttrade-bot both
```

## Notes
- `predictions.json` and `models.pkl` are written inside the container at `/app/zerodha_bot`; mount a volume if persistence is required.
- Set `DRY_RUN=true` to validate order-flow logic without placing orders.
