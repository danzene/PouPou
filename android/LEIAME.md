# Android (TWA) do PouPou

Projeto gerado pelo Bubblewrap a partir de `twa-manifest.json` (pacote `br.app.poupou`,
abre `https://poupou-three.vercel.app`). O guia da loja está em `docs/PLAY-STORE.md`.

Ferramentas (já instaladas nesta máquina em `C:\Users\User\.bubblewrap`, com
`config.json` apontando JDK 17 e Android SDK):

```bash
npm i -g @bubblewrap/cli
```

## Gerar um novo .aab

1. Em `twa-manifest.json`, aumente `appVersionCode` (+1) e `appVersionName`.
2. Dentro desta pasta:

```bash
bubblewrap update
```

```bash
bubblewrap build
```

As senhas pedidas estão em `keystore-senha.txt`. Sai `app-release-bundle.aab` (suba este)
e `app-release-signed.apk` (para instalar direto num celular de teste).

Sem a CLI, o mesmo build na mão (PowerShell):

```powershell
$env:JAVA_HOME = "C:\Users\User\.bubblewrap\jdk-17.0.20.1+1"
.\gradlew.bat bundleRelease
& "$env:JAVA_HOME\bin\jarsigner.exe" -keystore poupou-upload.keystore app\build\outputs\bundle\release\app-release.aab poupou
```

`poupou-upload.keystore`, `keystore-senha.txt`, `local.properties` e as pastas de build
ficam fora do Git (ver `.gitignore`).
