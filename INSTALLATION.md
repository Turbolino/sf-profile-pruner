# Guida all'installazione di Profile Pruner (Windows)

Questa guida spiega come installare **Profile Pruner** in modo pulito su un PC
Windows e come rendere disponibile il comando `sfpp` da terminale.

Il pacchetto si chiama **`sf-profile-pruner`**; `sfpp` è il *comando* che il
pacchetto installa.

> **Sintomo più comune**: dopo `pip install sf-profile-pruner`, il comando
> `sfpp` non viene riconosciuto (`impossibile trovare il file`). Non è un
> errore del pacchetto: l'eseguibile `sfpp.exe` è installato, ma la cartella
> `Scripts` del Python non è nel `PATH`. La FASE 4 sotto risolve il problema.

---

## FASE 1 — Installare Python correttamente

1. Scarica l'ultima **Python 3.x** da <https://www.python.org/downloads/>
   (installer *Windows 64-bit*).
2. Esegui l'installer e — **passo fondamentale** — spunta:
   - ☑ **Add python.exe to PATH**
   - ☑ **Install launcher for all users** (consigliato)
3. Clicca **Install Now**.
4. **Chiudi e riapri il terminale**, poi verifica:

   ```powershell
   python --version
   ```

   Deve stampare qualcosa come `Python 3.13.x`.

> Se apre il **Microsoft Store** o il percorso contiene `WindowsApps`, stai
> usando il Python dello Store. Disinstallalo e rifai la FASE 1 con l'installer
> di python.org (quello con la spunta PATH).

---

## FASE 2 — Installare Profile Pruner

Usa sempre `python -m pip` (così usi *esattamente* il Python appena installato):

```powershell
python -m pip install --upgrade sf-profile-pruner
```

Attendi la riga `Successfully installed sf-profile-pruner-...`.

> Il messaggio `Requirement already satisfied` (a un secondo tentativo) è
> normale: significa che il pacchetto è già installato. Non c'entra con il PATH.

---

## FASE 3 — Trovare la cartella `Scripts`

```powershell
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```

**Copia** il percorso stampato (es.
`C:\Users\tuo\AppData\Local\Programs\Python\Python313\Scripts`). Verifica che
`sfpp.exe` sia lì:

```powershell
Get-ChildItem (python -c "import sysconfig; print(sysconfig.get_path('scripts'))") -Filter "sfpp*"
```

Se vedi `sfpp.exe`, il pacchetto è installato: manca solo il `PATH`.

---

## FASE 4 — Aggiungere la cartella `Scripts` al `PATH` utente

### Metodo A — tramite interfaccia (funziona sempre)

Questo metodo funziona anche quando PowerShell è in **Constrained Language
Mode** (che blocca i metodi .NET come `[Environment]::SetEnvironmentVariable`).

1. Premi **Win + R**.
2. Digita **`sysdm.cpl`** e premi **Invio**.
3. Vai alla scheda **Avanzate**.
4. Clicca **Variabili d'ambiente...**.
5. Nella parte alta (*Variabili utente per `<tuo nome>`*), seleziona **`Path`**.
6. Clicca **Modifica...**.
7. Clicca **Nuovo**.
8. **Incolla** la cartella `Scripts` copiata nella FASE 3.
9. Clicca **OK** su tutte le finestre.

### Metodo B — da PowerShell (solo se non sei in modalità ristretta)

Sostituisci `<CARTELLA>` con il percorso della FASE 3:

```powershell
[Environment]::SetEnvironmentVariable(
  'Path',
  [Environment]::GetEnvironmentVariable('Path','User') + ';<CARTELLA>',
  'User'
)
```

Esempio:

```powershell
[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';C:\Users\tuo\AppData\Local\Programs\Python\Python313\Scripts', 'User')
```

> Se ricevi *«Impossibile richiamare il metodo... è supportato solo per i tipi
> di base»*, sei in Constrained Language Mode: usa il **Metodo A**.

---

## FASE 5 — Riaprire il terminale e verificare

Il `PATH` si aggiorna **solo in una nuova finestra**. Chiudi il terminale,
riaprilo e:

```powershell
sfpp --version
```

Deve stampare:

```
Profile Pruner v1.10.9 - Made with <3 by Lorenzo D'Aleo
(turbolino.dev@proton.me)
```

---

## Fallback senza toccare il `PATH`

Il tool funziona anche come modulo Python, quindi è utilizzabile **sempre**,
anche se `sfpp` non è nel `PATH`:

```powershell
python -m prune_profile --version
python -m prune_profile --help
```

## Test rapido (solo sessione corrente)

Se vuoi provare subito senza modificare nulla (sparisce alla chiusura del
terminale), sostituisci `<CARTELLA>` e digita:

```powershell
$env:Path += ";<CARTELLA>"
sfpp --version
```

---

## Diagnosi

Se `sfpp` non funziona ancora, esegui e riporta l'output di:

```powershell
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
where.exe sfpp
Get-ChildItem (python -c "import sysconfig; print(sysconfig.get_path('scripts'))") -Filter "sfpp*"
```

Per verificare se PowerShell è in modalità ristretta:

```powershell
$ExecutionContext.SessionState.LanguageMode
```

Se stampa `ConstrainedLanguage`, usa il **Metodo A** della FASE 4.
