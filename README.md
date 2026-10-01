# Moodle → Google Drive

Copia automáticamente todo tu Moodle a Google Drive y lo mantiene al día.

| En Moodle…                | En Drive…                                                         |
|---------------------------|-------------------------------------------------------------------|
| fichero nuevo             | se sube                                                           |
| fichero modificado        | se sustituye (la versión anterior queda en el historial de Drive) |
| fichero borrado           | **se mantiene** (su descripción indica cuándo se borró)           |
| fichero movido/renombrado | se mueve/renombra                                                 |

Copia recursos, carpetas, páginas, enlaces, tareas (enunciado, adjuntos, tu entrega y el
feedback), foros, calificaciones y un índice de cada curso, con la misma estructura de
secciones (y subpestañas) que en Moodle:

```
Moodle/<Curso>/<NN - Sección>/<NN - Subsección>/Tareas/<Tarea>/...
```

Cada cambio queda en el log indicando asignatura, sección y documento:

```
ACTUALIZADO | Asignatura: AABD | Sección: 03 - Erronka 1 / 05 - BD Aplikatua | Documento: Kafka.pdf
```

Requisito: [Docker Desktop](https://www.docker.com/products/docker-desktop/).
El Moodle debe tener activada la app móvil (casi todos la tienen).

## Instalación

### 1. Credenciales de Google (una sola vez, ~5 min)

1. En <https://console.cloud.google.com/> crea un proyecto.
2. Activa la **Google Drive API** (*APIs y servicios → Biblioteca*).
3. *Google Auth Platform → Público*: si usas una cuenta de universidad/empresa elige
   **Interno**. Si es una cuenta Gmail normal, elige **Externo** y pulsa **Publicar app**
   (si se queda en *Prueba*, la autorización caduca cada 7 días).
4. *Clientes → Crear cliente* → tipo **Aplicación de escritorio**. Copia el ID y el secreto.

### 2. Configuración

```bash
git clone <este repositorio>
cd <carpeta>
cp .env.example .env      # y rellénalo
```

### 3. Arrancar

```bash
docker compose up -d --build
docker compose logs -f
```

La primera vez aparece en el log un enlace **`>>> Abre este enlace…`**: ábrelo, entra con
tu cuenta de Google y acepta (si avisa de *app no verificada*: *Avanzado → Ir a…*).
A partir de ahí funciona solo.

Para que siga funcionando tras reiniciar el PC, activa en Docker Desktop
*Settings → General → Start Docker Desktop when you sign in*.

## Uso

```bash
docker compose logs -f      # ver qué hace
docker compose restart      # sincronizar ahora
docker compose down         # parar
```

- Frecuencia: `SYNC_INTERVAL_MINUTES` en `docker-compose.yml` (por defecto 2 min).
  Tras cambiarla: `docker compose up -d`.
- El permiso pedido a Google es `drive.file`: solo puede ver los ficheros que él mismo crea.
- Todo el estado se guarda en `data/` (`state.json`, `token.json`, `sync.log`).
  **No borres `state.json`**: sin él volvería a subirlo todo duplicado.
  Si borras `token.json`, te volverá a pedir la autorización.
