# Placeholder

## Despliegue en Render (gratuito)
1. Sube este repo a GitHub.
2. En https://render.com → **New +** → **Blueprint** → conecta tu repo (Render detecta `render.yaml`).
3. En Variables añade `TOKEN` y `TZ=Europe/Madrid` si no están.
4. Deploy. Cuando esté **Live**, añade el bot al grupo y usa `/start`.

### Mantener despierto (opcional)
Configura un ping cada 10 min a `https://<tu-servicio>.onrender.com/health` con cron-job.org.
