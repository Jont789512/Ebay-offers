# Ofertas eBay en GitHub Actions

Busca cada hora subastas de eBay (8 países de la UE) por debajo del valor de mercado y
envía un correo con las ofertas. Corre en los servidores de GitHub: no necesita tu PC.

## Configuración (una sola vez)

1. Crea un repositorio **privado** en GitHub y sube estos archivos (incluida la carpeta
   `.github`).
2. En el repositorio: **Settings > Secrets and variables > Actions > New repository secret**.
   Crea estos 5 secretos:

   | Nombre | Valor |
   |---|---|
   | `EBAY_CLIENT_ID` | App ID de eBay |
   | `EBAY_CLIENT_SECRET` | Cert ID de eBay |
   | `SMTP_USER` | Tu Gmail que envía |
   | `SMTP_PASSWORD` | Contraseña de aplicación de Gmail (16 letras, sin espacios) |
   | `EMAIL_TO` | Correo donde quieres recibir las ofertas |

3. Ve a la pestaña **Actions**, elige "Ofertas eBay" y pulsa **Run workflow** para probar.

Desde ahí se ejecuta solo cada hora.

## Cambiar búsquedas

Edita `config.json` directamente en GitHub (icono del lápiz) y guarda. La siguiente
ejecución ya usa los cambios.

## Notas

- **Kleinanzeigen está desactivado aquí**, porque suele bloquear las conexiones desde
  servidores en la nube. Para Kleinanzeigen usa la versión de tu PC.
- Las credenciales van solo en los Secrets, nunca en archivos del repositorio.
- GitHub puede retrasar las ejecuciones programadas unos minutos en horas de mucho uso.
- Un repositorio privado tiene 2.000 minutos gratis al mes; cada ejecución usa unos
  1 o 2 minutos, así que cada hora entra en el límite.
- GitHub desactiva las tareas programadas tras 60 días sin actividad en el repositorio;
  como el script guarda `enviados.json` cada vez que envía ofertas, normalmente se mantiene
  activo. Si se desactiva, vuelve a activarlo en la pestaña Actions.
