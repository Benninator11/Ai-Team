# KI-Team Studio Plus

**Ein Multi-Agent-Workspace für Linux**, in dem mehrere OpenRouter-KIs gemeinsam Software planen, bauen und überprüfen.

KI-Team Studio Plus verbindet vier konfigurierbare KI-Slots mit einer gemeinsamen Projektablage. Die Agenten stimmen ihre Arbeit über `CHAT.md`, `PLAN.md` und `tasks.json` ab.

## Funktionen

- **Vier KI-Slots:** Eigenständige Modell-, API-Key- und Rollen-Zuordnung pro Slot.
- **OpenRouter-Integration:** Lädt kostenlose Chat-Modellvarianten aus dem OpenRouter-Katalog und bietet einen automatischen Free-Router als Option.
- **Team-Meeting:** Die ausgewählten, einsatzbereiten KIs geben nacheinander Beiträge ab und beziehen sich dabei auf den bisherigen Gesprächsverlauf.
- **Geführte Planung:** Der Planner kann bis zu drei wichtige Rückfragen mit einer vorausgefüllten Empfehlung stellen.
- **Automatikmodus:** Die KI trifft technische Planungsentscheidungen selbst und hält relevante Annahmen im Plan fest.
- **Gemeinsamer Projekt-Workflow:** Aufgaben werden geplant, von den Bau-KIs umgesetzt und anschließend von Debug-KIs geprüft.
- **Dark Mode:** Umschaltbares helles und dunkles Farbschema; die Einstellung wird gespeichert.
- **Aktivitätsanzeige:** Dezente Animation signalisiert, wenn das Team arbeitet.
- **Automatisches Speichern:** Modellwahl, Rollen, Key-Zuordnungen und weitere Einstellungen werden lokal gespeichert.
- **Projektdateien:** Der Workflow verwendet `CHAT.md`, `PLAN.md` und `tasks.json` im gewählten Projektordner.

## Voraussetzungen

- Linux Mint oder eine andere Linux-Distribution
- Python 3
- Tkinter
- Ein OpenRouter-API-Key

Unter Ubuntu/Linux Mint lassen sich die benötigten Python-Komponenten installieren mit:

```bash
sudo apt update
sudo apt install python3 python3-tk
```

## Start

1. Repository herunterladen oder klonen.
2. Im Programmordner ein Terminal öffnen.
3. Starten:

```bash
python3 ki_team.py
```

Alternativ:

```bash
chmod +x start_ki_team.sh
./start_ki_team.sh
```

## Erste Einrichtung

1. OpenRouter-API-Key in einem der Key-Felder eintragen.
2. Für jeden KI-Slot ein Modell und den gewünschten Key auswählen.
3. Planner, Bauen und Debuggen zuweisen.
4. Zwischen geführter Planung und Automatikmodus wählen.
5. Projektordner und Auftrag festlegen und den Lauf starten.

Die Modellauswahl wird beim Abruf aus OpenRouter aktualisiert. Verfügbarkeit und kostenlose Endpunkte können sich ändern. Ein kostenloses Modell kann zeitweise ausgelastet, regional nicht verfügbar oder durch den Anbieter eingeschränkt sein.

## Einstellungen und API-Key-Sicherheit

Die Anwendung speichert ihre Konfiguration lokal unter:

```
~/.config/ki-team/config.json
```

Wenn das Speichern von API-Keys aktiviert ist, werden diese lokal mit restriktiven Dateirechten gespeichert (Dateimodus `0600`). **API-Keys niemals in Git committen oder öffentlich teilen.** Die lokale Konfigurationsdatei gehört nicht ins Repository.

## Projektdateien

| Datei | Zweck |
|---|---|
| `ki_team.py` | Hauptprogramm |
| `start_ki_team.sh` | Startskript für Linux |
| `README.md` | Dokumentation |
| `DESCRIPTION.md` | Kurzbeschreibung des Projekts |

## Hinweise zum Ablauf

- Die KI-Agenten kommunizieren über gemeinsam gelesene und geschriebene Projektdateien; es handelt sich nicht um einen permanenten Echtzeit-Gruppenchat.
- Team-Meetings und Planungsrückfragen erzeugen zusätzliche OpenRouter-Anfragen.
- Kostenlose Modelle können Rate-Limits, Wartezeiten oder regionale Einschränkungen haben.
- Vor einem produktiven Einsatz sollten die von den KIs erzeugten Änderungen geprüft und wichtige Projektdateien gesichert werden.

## Lizenz

Für dieses Repository wurde bislang keine Lizenzdatei hinzugefügt. Bitte vor Weiterverwendung oder Weiterverteilung die Lizenzbedingungen klären.
