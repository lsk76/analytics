# tg-event-analytics — Docker management

DC      = docker compose
DC_PROD = docker compose -f docker-compose.prod.yml

.PHONY: help dev prod build start stop restart logs ps shell dbshell \
        migrate makemigrations superuser changepassword collectstatic \
        seed run backup restore list-backups prod-build prod-logs prod-stop clean \
        workers workers-logs workers-stop scale-workers worker

# Default
help:
	@echo "tg-event-analytics — Docker commands:"
	@echo ""
	@echo "  Dev (runserver, live reload, port 8001):"
	@echo "    make dev          - up (foreground, logs)"
	@echo "    make build        - build + up (detached)"
	@echo "    make start        - up (detached)"
	@echo "    make stop         - down (data kept in volume)"
	@echo "    make restart      - down + up (detached)"
	@echo "    make logs         - follow web logs"
	@echo "    make ps           - container status"
	@echo ""
	@echo "  Prod (gunicorn, DEBUG=false, port 8001):"
	@echo "    make prod         - build + up (detached)"
	@echo "    make prod-logs    - follow web logs"
	@echo "    make prod-stop    - down"
	@echo ""
	@echo "  Django:"
	@echo "    make migrate      - apply migrations"
	@echo "    make makemigrations"
	@echo "    make superuser    - create admin user"
	@echo "    make changepassword USER=admin"
	@echo "    make collectstatic"
	@echo "    make shell        - Django shell"
	@echo "    make dbshell      - psql shell"
	@echo ""
	@echo "  Pipeline:"
	@echo "    make seed         - seed regions + nationalities + ethnic-clashes task"
	@echo "    make run SLUG=ethnic-clashes FROM=2026-05-26 TO=2026-06-03"
	@echo ""
	@echo "  Backup:"
	@echo "    make backup       - pg_dump -> backups/<timestamp>.dump"
	@echo "    make list-backups"
	@echo "    make restore FILE=backups/<name>.dump"

# ---------- Dev ----------
dev:
	$(DC) up

build:
	$(DC) up -d --build

start:
	$(DC) up -d

stop:
	$(DC) down

restart:
	$(DC) down
	$(DC) up -d

logs:
	$(DC) logs -f web

ps:
	$(DC) ps

# ---------- Workers (stage machine) ----------
WORKERS = worker-collect worker-enrich worker-precluster worker-classify worker-dedup

workers:                 # (re)start all stage workers (detached)
	$(DC) up -d $(WORKERS)

workers-logs:            # follow all worker logs
	$(DC) logs -f $(WORKERS)

workers-stop:
	$(DC) stop $(WORKERS)

scale-workers:           # e.g. make scale-workers ENRICH=2 CLASSIFY=2
	$(DC) up -d --scale worker-enrich=$(or $(ENRICH),1) --scale worker-classify=$(or $(CLASSIFY),1)

worker:                  # ad-hoc single pass: make worker STAGE=collect
	@if [ -z "$(STAGE)" ]; then echo "Usage: make worker STAGE=collect|enrich|precluster|classify|dedup"; exit 1; fi
	$(DC) exec web python manage.py run_worker --stage $(STAGE) --once

# ---------- Prod ----------
prod: prod-build
prod-build:
	$(DC_PROD) up -d --build

prod-logs:
	$(DC_PROD) logs -f web

prod-stop:
	$(DC_PROD) down

# ---------- Django ----------
migrate:
	$(DC) exec web python manage.py migrate

makemigrations:
	$(DC) exec web python manage.py makemigrations

superuser:
	$(DC) exec web python manage.py createsuperuser

changepassword:
	$(DC) exec web python manage.py changepassword $(or $(USER),admin)

collectstatic:
	$(DC) exec web python manage.py collectstatic --noinput

shell:
	$(DC) exec web python manage.py shell

dbshell:
	$(DC) exec db psql -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-tg_events}

# ---------- Pipeline ----------
seed:
	$(DC) exec web python manage.py seed_regions
	$(DC) exec web python manage.py seed_nationalities
	$(DC) exec web python manage.py seed_ethnic_clashes

run:
	@if [ -z "$(SLUG)" ] || [ -z "$(FROM)" ] || [ -z "$(TO)" ]; then \
		echo "Usage: make run SLUG=ethnic-clashes FROM=2026-05-26 TO=2026-06-03"; exit 1; \
	fi
	$(DC) exec web python manage.py run_analysis $(SLUG) --from $(FROM) --to $(TO)

# ---------- Backup ----------
backup:
	@mkdir -p backups
	@ts=$$(date +%Y%m%d_%H%M%S); \
	$(DC) exec -T db pg_dump -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-tg_events} -Fc > backups/$$ts.dump; \
	echo "Backup -> backups/$$ts.dump ($$(du -h backups/$$ts.dump | cut -f1))"

list-backups:
	@ls -lh backups/*.dump 2>/dev/null || echo "No backups yet"

restore:
	@if [ -z "$(FILE)" ]; then \
		echo "Usage: make restore FILE=backups/<name>.dump"; \
		$(MAKE) list-backups; exit 1; \
	fi
	@echo "Restoring $(FILE) into tg_events (existing data will be replaced)..."
	$(DC) exec -T db pg_restore -U $${POSTGRES_USER:-postgres} -d $${POSTGRES_DB:-tg_events} --clean --if-exists < $(FILE)
	@echo "Restored."

clean:
	$(DC) down -v
	@echo "Containers + pgdata volume removed."
