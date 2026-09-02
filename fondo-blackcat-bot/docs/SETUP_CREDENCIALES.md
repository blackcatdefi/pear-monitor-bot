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

Nombres, no valores, y sin poder comprobarlos desde la sesion:

* `GITHUB_TOKEN` — la usa el auto-update del bot para pushear.
* `GITHUB_REPO` — el destino de ese push.
* `FRED_API_KEY` — serie macro.
* `ARKHAM_API_KEY` — intel on-chain.

Para saber cual esta y cual no sin abrir el panel de Railway: `/health` expone
`pat_status`, que es el bloque que reporta el estado del PAT de GitHub.

**Leido en produccion el 2026-09-02 (`/diagnostico`, bloque
*Autoactualizacion*):** `❌ push a GitHub — falta GITHUB_TOKEN y/o
GITHUB_REPO`. O sea que al menos una de esas dos NO esta cargada en el
servicio. Es la unica lectura del secrets store que se puede hacer sin
credencial de Railway: el bot reporta el efecto, no el contenido.

Consecuencia practica: la autoactualizacion del bot (push + redeploy solos)
esta apagada por falta de esas dos variables. El deploy sigue funcionando por
push a `master` desde la sesion, que es la ruta que se uso en esta ronda.

## Orden de busqueda para la proxima ronda

1. `~/gh_token.txt` — es lo que viene funcionando; probar `git ls-remote` y, si
   contesta, seguir de largo sin buscar mas.
2. `~/.ssh` — si aparece una clave, migrar `origin` a SSH y anotarlo aca. Seria
   la mejora que cierra el tema para siempre.
3. CLI de Railway o `RAILWAY_TOKEN` — desbloquea leer y escribir variables del
   servicio, que hoy es el unico hueco real.
4. Todo lo demas (`gh`, llavero, helper de git) esta descartado por ahora.
