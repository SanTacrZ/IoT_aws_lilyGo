# CRUD — Diseño para pequeño productor, escalable a gran productor

> Principio: **una sola API**. El pequeño productor la usa con 1 clave; el gran productor
> con usuarios/roles sin cambiar nada de infraestructura.

## 1. Recursos y matriz de endpoints (`/api/v2`, JSON)

| Recurso | GET (list) | GET (one) | POST | PATCH | DELETE (lógico) |
|---|---|---|---|---|---|
| farms | `/farms?limit&offset` | `/farms/<id>` | ✅ | ✅ | ✅ (409 si tiene zonas; `?force=1`) |
| zones | `/zones?farm_id` | `/zones/<id>` | ✅ | ✅ | ✅ (`enabled=false`) |
| devices | `/devices?zone_id&status` | vía `/state` | *(auto-alta por POST de placa)* | ✅ (name, zone_id, enabled) | ✅ |
| sensors | `/devices/<id>/sensors` | `/devices/<id>/sensors/<sid>/health` | *(auto-alta)* | — | `DELETE /devices/<id>/sensors/<sid>` |
| actuators | `/actuators?device_id` | — | ✅ | ✅ (label, pin, enabled) | ✅ |
| rules | `/rules?zone_id` | — | ✅ | ✅ | ✅ (`enabled=false`) |
| alerts | `/alerts?open=1` | — | *(motor de reglas)* | — | — (se **ackea**: `POST /alerts/<id>/ack`) |
| users | `/users` | — | ✅ | — | — |
| readings | `/history?device_id&sensor_id&limit` | — | *(firmware HMAC)* | — | — |
| riego manual | — | — | `POST /zones/<id>/irrigate {duration_min}` | — | — |

Convenciones: paginación `?limit&offset` + header `X-Total-Count`; errores
`{"error": "..."}` con 400/401/404/409/422; borrados SIEMPRE lógicos (nunca se pierde
historia); `PATCH` acepta campos parciales.

## 2. Autenticación y roles (el "pequeño → grande")

**Fase pequeña productor (hoy):**
- Dispositivos: `X-Api-Key` + HMAC (ya implementado, anti-replay).
- El dueño administra con **una clave**: header `X-Admin-Key: <ADMIN_KEY>` (env del servidor).
  Cero fricción: no registrar usuarios, no contraseñas que olvidar.

**Fase gran productor (diseñada, sin reescribir):**
- Login humano → **JWT** (o AWS Cognito/Google OIDC). El mismo backend valida el token y
  mapea `role` → permisos. Los endpoints son los mismos; solo cambia el guard:
  `admin_required()` → `auth_required(perm="zones:write")`.
- RBAC sobre `users` + `farm_members` (tabla ya creada en 002_precision.sql):

| Permiso | viewer | farmer | admin |
|---|---|---|---|
| Ver dashboards/historia/alertas | ✅ | ✅ | ✅ |
| CRUD zonas/actuadores/reglas, ack alertas, riego manual | — | ✅ | ✅ |
| CRUD farms, DELETE devices, gestionar users | — | — | ✅ |

- Aislamiento: toda query filtra por `farm_id ∈ farms del usuario` (multi-tenancy por fila).

## 3. Flujo típico del pequeño productor (5 minutos, sin tocar código)

```
1. POST /api/v2/farms {"name":"Mi parcela"}
2. POST /api/v2/zones {"farm_id":1,"name":"Tomate","crop":"tomate"}   ← umbrales default sanos
3. Enciende la placa → POST automático del firmware → device+sensors se registran solos
4. PATCH /api/v2/devices/lilygo-01 {"zone_id":1}   ← asigna placa a la zona
5. POST /api/v2/actuators {"device_id":"lilygo-01","zone_id":1,"kind":"pump","label":"bomba1","pin":26}
6. POST /api/v2/rules {"zone_id":1,"name":"riego","condition":"below","threshold":35,"action":"irrigate+alert"}
7. Abre /dashboard-v2 → ve todo en tiempo real. Motor de reglas riega solo.
   Botón "Riego manual" → POST /zones/1/irrigate {"duration_min":10}
```

## 4. Escalado (pequeño → grande) sin rediseño

| Dimensión | Pequeño | Grande | Mecanismo |
|---|---|---|---|
| Usuarios | 1 admin-key | N usuarios + roles | tabla users ya existe; cambiar guard |
| Parcelas | 1 | miles | `farm_id` FK en todo; paginación siempre |
| Dispositivos | 1–5 | 10k+ | auto-alta ya lo tolera; índice `(device_id,sensor_id,ts DESC)` |
| Datos | MB | TB | TimescaleDB hipertable+compresión+retención (en seed y RDS) |
| Trafico API | 1 placa | 10k placas | rate-limit por device (ya); Fargate escala replicas |
| Costo | ~$0 (docker local) | ~$50/mes inicial | mismo stack; solo cambian tamaños/replicas |

Puntos de escalado previstos: read-replica de RDS para dashboards analíticos,
SQS entre ingesta e inserción si hay picos, partitioning por farm si multi-región.
Nada de esto cambia el CRUD ni el firmware.

## 5. Seguridad

- Dispositivos: HMAC+timestamp (replay imposible), rate-limit, validación de rangos.
- Humanos: `X-Admin-Key` sobre **HTTPS** (en AWS, ALB+ACM); nunca en URLs ni logs.
- Bajas lógicas: un "borrado" accidentale es reversible (`enabled=true`).
- Auditoría: `irrigation_events` registra trigger/manual/regla; TODO: tabla `audit_log`
  (fase JWT) con `user_id, acción, recurso, ts`.
- SQL siempre parametrizado (psycopg2 %s) — sin concatenación.
