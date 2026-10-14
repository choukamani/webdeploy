# Modes de Deploiement — Architecture Detaillee

Ce document explique les differents modes de deploiement de la plateforme WebDeploy,
leur architecture reseau/GCP, et les concepts fondamentaux de routing.

---

## Vue d'ensemble

Depuis octobre 2026, **tous les nouveaux sites** passent par un load balancer
dedie a la plateforme, `webdeploy-sites`. `legacy-demo-lb` approchait la limite
GCP de taille d'une URL map (64 kB) et le quota de backend buckets du projet. Les sites existants restent sur leur LB d'origine pour que leurs URLs
ne changent pas ; les sections 1 a 3 ci-dessous decrivent ces placements
historiques.

| Mode | URL (nouveau site) | Routage | SSL | Ressources par site |
|------|--------------------|---------|-----|---------------------|
| **Demo** | `demo.example.com/mon-site/` | Path-based, bucket **partage** | Wildcard `*.example.com` | Un prefixe `mon-site/` dans le bucket partage |
| **Production** | `mon-domaine.com` | Host-based | Certificate Manager, un cert par domaine | Bucket, backend bucket, host rule, cert, DNS |
| **Cloud Run** | `mon-app.example.com` | Host-based → serverless NEG | Wildcard | Service, NEG, backend service, host rule, DNS A |

### LB dedie `webdeploy-sites`

```
IP webdeploy-sites-ip ──┬── :443 HTTPS proxy ── certificate map
                        │         │               ├─ PRIMARY + *.example.com → cert wildcard (autorisation DNS)
                        │         │               └─ mon-domaine.com → cert du domaine (autorisation LB)
                        │         ▼
                        │     URL map webdeploy-sites-urlmap
                        │       ├─ demo.example.com → backend bucket partage → gs://…-webdeploy-demo-sites/{site}/
                        │       ├─ mon-app.example.com → backend service → NEG serverless → Cloud Run
                        │       └─ mon-domaine.com → backend bucket du domaine → bucket du domaine
                        └── :80 → redirection 301 vers HTTPS
```

Un site demo n'ajoute **rien** a l'URL map ni aux backend buckets : il occupe un
prefixe d'objets dans le bucket partage, servi par l'unique backend bucket demo.
Seuls les domaines prod et les apps Cloud Run ajoutent une entree (~150 octets),
soit plusieurs centaines de sites avant d'approcher la limite de 64 kB.

Le mode **Subdomain** (section 2) n'est plus deployable : le formulaire
« Subdomain » cree un site Production sur le FQDN compose. Les deux sites
historiques de ce mode restent supprimables.

---

## 1. Mode Demo — Path-Based Routing

### Concept

Tous les sites demo partagent un **seul domaine** (`example.com`) et un
**seul load balancer**. Chaque site est differencie par son **chemin URL** (path).

### Architecture GCP

```
Client → https://example.com/portfolio/index.html
         │
         ▼
   IP Statique Globale (legacy-demo-lb-ip)
         │
         ▼
   Forwarding Rule (HTTPS:443)
         │
         ▼
   Target HTTPS Proxy ←── SSL Cert (example.com)
         │
         ▼
   URL Map (legacy-demo-lb)
     │
     ├─ Host: example.com
     │   └─ Path Matcher: main-matcher
     │       ├─ /portfolio/*     → demo-portfolio-backend-demo     → gs://demo-portfolio-bucket-demo
     │       ├─ /ecommerce/*     → demo-ecommerce-backend-demo     → gs://demo-ecommerce-bucket-demo
     │       └─ /client-site/*   → demo-client-site-backend-demo   → gs://demo-client-site-bucket-demo
```

### Ressources creees par deploiement

| Ressource | Nommage | Partagee ? |
|-----------|---------|------------|
| GCS Bucket | `demo-{safe_name}-bucket-demo` | Non (1 par site) |
| Backend Bucket | `demo-{safe_name}-backend-demo` | Non (1 par site) |
| Path Rule dans URL Map | `/{website_name}/*` | Ajoutee au URL Map partage |
| SSL Certificate | `example.com` | Oui (1 pour tous) |
| IP Globale | `legacy-demo-lb-ip` | Oui |

### Comment le routing fonctionne

1. Le navigateur envoie une requete a `example.com/portfolio/page.html`
2. Le DNS resout `example.com` vers l'IP statique du load balancer
3. Le load balancer recoit la requete HTTPS (SSL termine au proxy)
4. Le **URL Map** examine le **path** (`/portfolio/page.html`)
5. La regle `/portfolio/*` matche → requete routee vers `demo-portfolio-backend-demo`
6. Le backend bucket sert le fichier depuis `gs://demo-portfolio-bucket-demo/portfolio/page.html`

### Structure des fichiers dans le bucket

```
gs://demo-portfolio-bucket-demo/
  └── portfolio/
      ├── index.html
      ├── assets/
      │   ├── main.js
      │   └── style.css
      └── images/
          └── logo.png
```

Les fichiers sont uploades dans un sous-dossier `/{website_name}/` car le path
dans l'URL correspond au prefix du bucket.

---

## 2. Mode Subdomain — Host-Based Routing sur Sous-domaine

### Concept

Chaque site obtient un **sous-domaine** d'un domaine existant. Le routing se
fait par **hostname** (pas par path), ce qui donne une URL propre et professionnelle.

Deux cas sont supportes automatiquement :

### Cas A — Subdomain interne (example.com)

URL: `portfolio.example.com`
- Utilise le **demo LB** (partage)
- SSL via **certificat wildcard** `*.example.com`
- DNS via **record wildcard** → aucune action utilisateur

```
Client → https://portfolio.example.com/index.html
         │
         ▼
   IP Statique Globale (legacy-demo-lb-ip)
         │
         ▼
   Target HTTPS Proxy ←── SSL Wildcard (*.example.com)
         │
         ▼
   URL Map (legacy-demo-lb)
     ├─ Host: example.com           ← Demo path-based
     ├─ Host: portfolio.example.com  ← Subdomain interne
     │   └─ pm-sub-portfolio → sub-portfolio-backend-demo
     └─ Host: ecommerce.example.com
         └─ pm-sub-ecommerce → sub-ecommerce-backend-demo
```

### Cas B — Subdomain externe (domaine client)

**Exemple** : Le client possede `client-domain.com` chez GoDaddy et veut
deployer `partners.client-domain.com`

URL: `partners.client-domain.com`
- Utilise le **prod LB** (partage)
- SSL via **certificat individuel** Google-managed pour le FQDN
- DNS : l'utilisateur doit ajouter un **record A** chez son registrar

```
Client → https://partners.client-domain.com
         │
         ▼
   IP Statique Globale (websites-lb-ip-prod)
         │
         ▼
   Target HTTPS Proxy (websites-https-proxy-prod)
     ├── SSL: partners-client-domain-com-ssl-cert
     ├── SSL: (autres certs prod)
         │
         ▼
   URL Map (websites-urlmap-prod)
     ├─ Host: client-site.com                              ← Prod classique
     ├─ Host: partners.client-domain.com            ← Subdomain externe !
     │   └─ pm-partners-client-domain-com
     │       → sub-become-a-partner-backend-demo
     │         → gs://sub-become-a-partner-bucket-demo
     └─ Host: reviews.client-domain.com
         └─ pm-reviews-client-domain-com
             → sub-share-your-recommendations-backend-demo
```

**Action requise apres deploiement** (Cas B uniquement) :

L'utilisateur doit aller chez son registrar et creer :
```
partners.client-domain.com  A  34.8.x.x    (IP du prod LB)
```

Le certificat SSL se provisionne automatiquement une fois le DNS configure
(peut prendre jusqu'a 24h).

### Difference cle avec le mode Demo

| Aspect | Demo (Path) | Subdomain Interne | Subdomain Externe |
|--------|-------------|-------------------|-------------------|
| URL | `example.com/site/` | `site.example.com` | `site.client.com` |
| LB utilise | Demo | Demo | Prod |
| SSL | 1 cert root | Wildcard partage | Cert individuel FQDN |
| Upload GCS | Dans `/{site}/` | A la racine `/` | A la racine `/` |
| Action DNS | Aucune | Aucune | Record A chez registrar |

### Ressources creees par deploiement

| Ressource | Nommage | Partagee ? |
|-----------|---------|------------|
| GCS Bucket | `sub-{safe_name}-bucket-demo` | Non (1 par site) |
| Backend Bucket | `sub-{safe_name}-backend-demo` | Non (1 par site) |
| Host Rule | `{fqdn}` dans URL Map | Ajoutee (demo ou prod) |
| Path Matcher | `pm-sub-{safe_name}` ou `pm-{safe_fqdn}` | Non |
| SSL (interne) | Wildcard existant | Oui |
| SSL (externe) | `{safe_fqdn}-ssl-cert` | Non |

### Pre-requis (one-time setup pour Cas A)

1. **Certificat SSL wildcard** : `*.example.com` attache au demo HTTPS proxy
2. **DNS wildcard** : `*.example.com` A → IP du demo LB

### Structure des fichiers dans le bucket

```
gs://sub-portfolio-bucket-demo/
  ├── index.html          ← A la racine ! Pas dans un sous-dossier
  ├── assets/
  │   ├── main.js
  │   └── style.css
  └── images/
      └── logo.png
```

Contrairement au mode demo, les fichiers sont a la **racine du bucket** car
l'URL `portfolio.example.com/index.html` n'a pas de prefix de path.

---

## 3. Mode Production — Custom Domain

### Concept

Chaque site a son **propre domaine** (`client-site.com`). La plateforme cree
les certificats SSL, les zones DNS, et ajoute le domaine au load balancer
de production partage.

### Architecture GCP

```
Client → https://client-site.com/index.html
         │
         ▼
   IP Statique Globale (websites-lb-ip-prod)
         │
         ▼
   Forwarding Rule (HTTPS:443)
         │
         ▼
   Target HTTPS Proxy (websites-https-proxy-prod)
     ├── SSL Cert: client-site-com-ssl-cert
     ├── SSL Cert: other-domain-ssl-cert
     └── ...
         │
         ▼
   URL Map (websites-urlmap-prod)
     ├─ Host: client-site.com
     │   └─ Path Matcher: pm-client-site-com
     │       └─ defaultService → client-site-com-backend-prod
     │                            → gs://client-site-com-bucket-prod
     │
     └─ Host: other-domain.fr
         └─ Path Matcher: pm-other-domain-fr
             └─ defaultService → other-domain-fr-backend-prod
```

### Ressources creees par deploiement

| Ressource | Nommage | Partagee ? |
|-----------|---------|------------|
| GCS Bucket | `{safe_domain}-bucket-prod` | Non |
| Backend Bucket | `{safe_domain}-backend-prod` | Non |
| SSL Certificate | `{safe_domain}-ssl-cert` | Non |
| DNS Zone | `{safe_domain}-zone` | Non |
| A Record | `{domain}.` → IP du LB | Non |
| CNAME Record | `www.{domain}.` → `{domain}.` | Non |
| Host Rule | `{domain}` dans URL Map partage | Ajoutee |
| Path Matcher | `pm-{safe_domain}` | Non |

### Etapes du deploiement Production

1. Recuperer l'IP du LB partage (`websites-lb-ip-prod`)
2. Creer le bucket GCS + permissions publiques
3. Creer le backend bucket (CDN active)
4. Ajouter host rule + path matcher au URL Map partage
5. Creer le certificat SSL Google-managed
6. Attacher le cert au HTTPS proxy partage
7. Creer la zone DNS + records A et CNAME
8. Uploader les fichiers
9. Invalider le cache CDN

### Workflow DNS (domaine externe)

Si le domaine est enregistre chez un registrar externe (GoDaddy, OVH...) :

```
1. WebDeploy cree une zone DNS dans Cloud DNS
2. Cloud DNS fournit des nameservers (ns1.google.com, ns2.google.com, etc.)
3. L'utilisateur va chez son registrar et change les NS pour pointer vers Google
4. Google gere ensuite le DNS (A record → IP du LB)
5. Le certificat SSL se provisionne automatiquement (validation DNS)
```

---

## 4. Mode Cloud Run — Applications Conteneurisees

### Concept

Pour les applications qui ne sont pas des sites statiques (Node.js, Python, etc.),
le code est conteneurise et deploye sur Cloud Run.

### Etapes specifiques

1. Detection du type de projet (Node.js, Python, Go, etc.)
2. Generation automatique d'un Dockerfile
3. Build de l'image Docker via Cloud Build
4. Push vers Artifact Registry
5. Deploiement sur Cloud Run

L'URL finale est geree par Cloud Run : `https://{service}-{hash}.run.app`

---

## Comparaison des couts

| Mode | IP | SSL | DNS | CDN | Total/mois |
|------|-----|------|------|------|------------|
| Demo | Partage | 0 $ (1 cert) | 0 $ | Partage | ~0 $ supplementaire |
| Subdomain | Partage | 0 $ (wildcard) | 0 $ (wildcard A) | Partage | ~0 $ supplementaire |
| Production | Partage | ~0.75 $/cert | ~0.20 $/zone | Partage | ~1 $/domaine |
| Cloud Run | N/A | Inclus | N/A | N/A | ~0-5 $/service |

---

## Glossaire

- **URL Map** : Composant GCP qui route les requetes HTTP(S) vers les backend buckets en fonction du hostname et du path
- **Host Rule** : Regle dans le URL Map qui matche un hostname (ex: `site.com`)
- **Path Matcher** : Ensemble de regles de path associe a un host rule
- **Path Rule** : Regle qui matche un pattern de path (ex: `/portfolio/*`)
- **Backend Bucket** : Ressource GCP qui connecte un URL Map a un bucket GCS (avec CDN)
- **SSL Certificate (managed)** : Certificat SSL provisionne et renouvele automatiquement par Google
- **Wildcard SSL** : Certificat qui couvre `*.domain.com` (tous les sous-domaines d'un niveau)
- **Forwarding Rule** : Regle qui associe une IP:port a un target proxy
- **Target HTTPS Proxy** : Proxy qui termine le SSL et route vers le URL Map
- **CDN** : Cache distribue mondialement qui accelere les temps de chargement
- **safe_name** : Fonction qui convertit un nom en format compatible GCP (lowercase, hyphens, 63 chars max)
