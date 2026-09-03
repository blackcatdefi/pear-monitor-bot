# Ruta de credenciales — que anda, que no, y donde mirar primero

> **Regla que este archivo respeta y que no se negocia:** aca no hay ni va a
> haber un solo valor de secreto. Solo NOMBRES de variables, NOMBRES de archivos
> y RUTAS. Si alguna vez alguien pega un token aca, el token esta quemado y hay
> que rotarlo, no borrar el commit.

## Por que existe este archivo

Cuatro rondas seguidas se trabaron en el mismo punto: el trabajo quedaba hecho y
pusheado, y la verificacion contra produccion no se podia cerrar porque en la
sesion no habia credenciales, o las habia y se descubria tarde. Cada ronda
volvia a buscar de cero en los mismos seis lugares.

El inventario de abajo es el resultado de esa busqueda. La proxima ronda no
tiene que repetirla: empieza por "lo que anda" y sigue.

## Lo que anda

**GitHub por token en archivo — ESTA ES LA RUTA BUENA.**

* Archivo: `~/gh_token.txt` (en el home de la sesion, fuera del repo).
* Uso: se arma la URL de push como
  `https://x-access-token:<contenido-del-archivo>@github.com/<owner>/<repo>.git`.
* Repo: `blackcatdefi/pear-monitor-bot`, rama `master`.
* Comprobado con `git ls-remote` (lee, no escribe) antes de cualquier push.
* Al imprimir la salida de un comando que lleve la URL, pasarla por `sed` para
  reemplazar el token por `<REDACTADO>`. El token aparece en la URL, o sea en el
  eco del comando y en los mensajes de error de git.

## Lo que NO existe en esta sesion

Verificado por nombre, no por suposicion. Si en una ronda futura alguna de estas
aparece, conviene preferirla al token en archivo — sobre todo SSH, que no
caduca y no se puede filtrar por eco de comando.

| Ruta | Estado | Como se comprobo |
|---|---|---|
| Clave SSH + `origin` por SSH | ausente | no existe `~/.ssh` |
| `gh` CLI | ausente | no esta en el `PATH` |
| CLI de Railway | ausente | no esta en el `PATH` |
| `RAILWAY_TOKEN` en el entorno | ausente | variable no definida |
| Config de Railway en el home | ausente | no hay `~/.railway` |
| Helper de credenciales de git / llavero | ausente | no hay helper configurado |

**Consecuencia directa:** no hay forma de leer ni de escribir variables del
servicio de Railway desde la sesion, ni de abrir una shell en el contenedor. Lo
que se puede verificar de produccion se verifica por Telegram, con `/health` y
`/diagnostico` contra el bot vivo. Por eso las dos fases anteriores empujaron
tanta telemetria a esos dos comandos: **son el unico canal de lectura de
produccion que existe.**

## Variables del servicio (`amusing-acceptance`)

Nombres, no valores. Desde R-RAILWAY-VARS la presencia SI se puede leer: la
reporta el propio bot en ***Claves de servicio*** (ver abajo). Estado leido en
produccion el **2026-09-03**:

* `GITHUB_TOKEN` — ✅ la usa el auto-update del bot para pushear. **La unica
  imprescindible. CARGADA el 2026-09-03 por BCD a mano en el panel**; desde
  entonces la autoactualizacion esta en verde.
* `GITHUB_REPO` — ❌ ausente, y no se requiere (ver abajo). Si se carga, gana.
* `FRED_API_KEY` — ✅ presente. Serie macro.
* `ARKHAM_API_KEY` — ❌ ausente. Intel on-chain, degradacion conocida.

Para saber cual esta y cual no sin abrir el panel de Railway hay dos lecturas.
`/health` expone `pat_status`, y desde R-RAILWAY-VARS `/diagnostico` trae el
bloque ***Claves de servicio***, que lista las cuatro por nombre con ✅/❌. Ese
bloque cierra el hueco que este archivo venia documentando: la respuesta a
"¿que claves estan cargadas?" ahora es de primera mano, la da el proceso que
corre en Railway, y no depende de ninguna credencial. Publica el nombre y un
booleano — nunca el valor, ni un prefijo, ni la longitud. Una variable creada
con el valor en blanco se reporta **ausente**, que es lo que significa para el
codigo que despues la usa.

**Leido en produccion el 2026-09-02 (`/diagnostico`, bloque
*Autoactualizacion*):** `❌ push a GitHub — falta GITHUB_TOKEN y/o
GITHUB_REPO`. Es la unica lectura del secrets store que se puede hacer sin
credencial de Railway: el bot reporta el efecto, no el contenido.

**Ese mensaje era falso en la mitad que importa (corregido en R-RAILWAY-VARS).**
El texto se elegia mirando SOLO el token, mientras el veredicto exigia token Y
repo. O sea que ese "y/o" no era una duda honesta: era una conjetura impresa
con formato de hallazgo. Lo que si prueba, porque esa rama depende unicamente
del token, es que **`GITHUB_TOKEN` NO esta cargada** en el servicio.

**Cerrado el 2026-09-03.** BCD cargo `GITHUB_TOKEN` en el panel y redeployo. El
mismo bloque ahora lee, textual: `✅ push a GitHub (via GITHUB_TOKEN)` y
`✅ redeploy en Railway — automatico por push a master`. Con `GITHUB_REPO`
todavia ausente, que es la prueba de campo de que nunca hizo falta.

**Y `GITHUB_REPO` nunca hizo falta.** Se leia en un solo lugar de todo el repo:
el chequeo que la exigia. Ningun camino de push la consume — el de backups usa
`GITHUB_BACKUP_REPO` y el del reconciler usa el remoto `origin` del propio
clon. Consecuencia cara: cargar solo el token, que es lo unico que de verdad
hace falta, NO habria puesto la linea en verde, y el diagnostico habria echado
la culpa a una variable que nadie tenia que cargar. Ahora el destino del push
se resuelve `GITHUB_REPO` → remoto `origin` → constante, asi que la unica
variable que queda por cargar es el token.

**`RAILWAY_TOKEN` tampoco es un faltante.** Solo sirve para forzar un redeploy
sin push; todos los deploys reales entraron por push a `master`. La linea de
redeploy ahora se apoya en `RAILWAY_GIT_COMMIT_SHA`, que Railway inyecta solo
si ella misma construyo el deploy que esta corriendo: es evidencia observada de
que el auto-deploy anda, no una suposicion.

Consecuencia practica: para encender la autoactualizacion alcanza con **una**
variable, `GITHUB_TOKEN`. Mientras tanto el deploy sigue entrando por push a
`master` desde la sesion, que es la ruta que se uso en esta ronda.

## Orden de busqueda para la proxima ronda

1. `~/gh_token.txt` — es lo que viene funcionando; probar `git ls-remote` y, si
   contesta, seguir de largo sin buscar mas.
2. `~/.ssh` — si aparece una clave, migrar `origin` a SSH y anotarlo aca. Seria
   la mejora que cierra el tema para siempre.
3. CLI de Railway o `RAILWAY_TOKEN` — desbloquea leer y escribir variables del
   servicio, que hoy es el unico hueco real.
4. Todo lo demas (`gh`, llavero, helper de git) esta descartado por ahora.
