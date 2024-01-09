# Letterboxd-Jellyfin Integration

Ce dépôt GitHub contient un projet open-source visant à intégrer la watchlist de Letterboxd avec Jellyfin en récupérant les TMDB ID.

## Fonctionnalités

- Récupération des films de la watchlist Letterboxd
- Recherche des TMDB ID correspondants pour chaque film
- Intégration des films dans Jellyfin en utilisant les TMDB ID

## Comment ça fonctionne

1. L'utilisateur autorise l'accès à sa watchlist Letterboxd
2. Le script récupère les films de la watchlist
3. Pour chaque film, le script recherche le TMDB ID correspondant
4. Les films sont ajoutés à Jellyfin en utilisant les TMDB ID