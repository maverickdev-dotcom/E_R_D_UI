# Utiliser une image Python officielle légère
FROM python:3.11-slim

# Définir le répertoire de travail dans le conteneur
WORKDIR /app

# Copier le fichier de dépendances et l'installer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le reste du projet (y compris app.py, dossiers static et templates)
COPY . .

# Exposer le port sur lequel l'application Flask écoute
EXPOSE 5050

# Lancer l'application web
CMD ["python", "app.py"]
