# PouPou na Google Play — passo a passo

O app da Play Store é uma **TWA** (Trusted Web Activity): uma "casca" Android que abre
`https://poupou-three.vercel.app` em tela cheia, sem barra de endereço. O conteúdo continua
vindo do site, então **toda atualização do site chega no app sozinha**. Só é preciso subir
um `.aab` novo se mudar nome, ícone, cor ou domínio.

Por que isso resolve o aviso "PouPou requer o seguinte app: Chrome": o ícone que o Chrome
instala (WebAPK) depende do Chrome. O app da Play é independente: abre com qualquer
navegador compatível (Chrome, Samsung Internet, Edge, Firefox…) e, se não houver suporte a
TWA, abre em Custom Tab.

---

## 1. Criar a conta de desenvolvedor (uma vez só)

1. Acesse <https://play.google.com/console/signup> com a conta Google que vai ser dona do app.
2. Escolha **Pessoal** (você) ou **Organização** (precisa de CNPJ + número D-U-N-S, mais demorado).
3. Pague a taxa única de **US$ 25** (cartão internacional).
4. Faça a verificação de identidade (documento com foto) e confirme e-mail e telefone.
   Pode levar de algumas horas a alguns dias.
5. **Conta pessoal nova:** o Google exige um **teste fechado com pelo menos 12 testadores
   por 14 dias seguidos** antes de liberar a produção. Junte 12 pessoas (família, amigos,
   clientes) com e-mail Google — veja o passo 4.

## 2. Criar o app no Play Console

1. **Criar app** → Nome: `PouPou` · Idioma padrão: Português (Brasil) · Tipo: **App** ·
   **Gratuito** · aceite as declarações.
2. Em **Configurar o app** preencha as tarefas pendentes:
   - **Política de privacidade:** URL pública (pode ser uma página no próprio site).
   - **Acesso ao app:** "Todas as funcionalidades estão disponíveis sem restrições" — ou, se o
     Premium exigir login, informe um usuário de teste.
   - **Anúncios:** Não.
   - **Classificação do conteúdo:** preencha o questionário (categoria Compras/Utilitário).
   - **Público-alvo:** 18+ (evita as regras de apps infantis).
   - **Segurança dos dados:** coleta e-mail (login), dados de compra (assinatura Mercado Pago),
     localização aproximada se usar; dados criptografados em trânsito; usuário pode pedir exclusão.
   - **Apps de finanças / saúde / governo:** Não.

## 3. Ficha da loja (Presença na loja → Versão principal da página)

| Campo | O que usar |
|---|---|
| Nome do app | `PouPou — Compare e economize` (máx. 30 caracteres) |
| Descrição curta | `Compare preços dos supermercados de Campinas e economize.` (máx. 80) |
| Descrição completa | Explique: compara 11+ supermercados de Campinas e região, preços atualizados todo dia, busca por nome ou código de barras, alerta de preço, cesta básica. |
| Ícone (512×512) | `android/store_icon.png` |
| Imagem de destaque (1024×500) | Precisa criar (logo + frase sobre fundo verde). |
| Capturas de celular (mín. 2) | `front/screenshots/home.png` e `front/screenshots/produtos.png` (tire mais pelo celular se quiser) |
| Categoria | Compras |
| E-mail de contato | `contato@poupou.app` |

## 4. Subir o `.aab`

O arquivo pronto (já assinado com a chave de upload) é `android/app-release-bundle.aab`.
Para testar antes num celular Android, instale `android/app-release-signed.apk` (mande o
arquivo pro celular e abra; permita "instalar apps desconhecidos"). Ele já abre em tela
cheia, porque a digital da chave de upload já está no `assetlinks.json` do site. O app
baixado da Play é outra história: veja o passo 5.

1. **Testes → Teste fechado** → criar faixa → **Criar versão**.
2. Na primeira vez o Console pergunta sobre a **Assinatura de apps do Google Play**:
   aceite (o Google guarda a chave definitiva; a nossa vira só a "chave de upload").
3. Envie o `app-release-bundle.aab`, dê um nome à versão (`1.0.0`) e salve.
4. Em **Testadores**, cole os e-mails (ou um Grupo do Google) e compartilhe o link de convite.
5. **Revisar versão → Iniciar lançamento**. Depois dos 14 dias com 12 testadores, peça
   acesso à **Produção** e promova a mesma versão.

## 5. Ligar o app ao site (obrigatório para sumir a barra de endereço)

Com Play App Signing, o Google re-assina o app com a chave **dele**. O site precisa conhecer
essa digital:

1. Play Console → seu app → **Testar e lançar → Configuração → Integridade do app →
   Assinatura de apps** (em inglês: *App integrity → App signing*).
2. Copie a **Impressão digital do certificado SHA-256** da seção **"Certificado da chave de
   assinatura do app"** (a da "chave de upload" já está no arquivo).
   - A mesma página tem um bloco **"Digital Asset Links JSON"** pronto — dá pra copiar de lá.
3. Adicione essa digital na lista `sha256_cert_fingerprints` de `front/assetlinks.json`
   (mantenha a que já está lá, separadas por vírgula), faça commit e deixe o Vercel publicar.
4. Confira: <https://poupou-three.vercel.app/.well-known/assetlinks.json> deve mostrar as duas
   digitais. Teste no validador do Google:
   <https://developers.google.com/digital-asset-links/tools/generator>
   (site `poupou-three.vercel.app`, pacote `br.app.poupou`, digital do Play).

Se o app abrir com uma barrinha mostrando o endereço no topo, é este passo que falta.

## 6. Atualizações futuras

- **Mudanças no site:** nada a fazer, o app pega sozinho.
- **Mudou ícone, nome, cor ou domínio:** edite `android/twa-manifest.json`, aumente
  `appVersionCode` (2, 3…) e `appVersionName`, rode `bubblewrap update` e `bubblewrap build`
  dentro de `android/` (detalhes em `android/LEIAME.md`) e suba o novo `.aab`.
- **Domínio próprio no futuro:** o app está amarrado a `poupou-three.vercel.app`. Trocar exige
  nova versão do app e o `assetlinks.json` publicado também no domínio novo.

## Arquivos que não podem se perder

`android/poupou-upload.keystore` e `android/keystore-senha.txt` (ficam fora do Git). Guarde
cópia num lugar seguro (Google Drive pessoal, pendrive). Se perder, dá pra pedir ao Google
a troca da chave de upload pelo Play Console, mas atrasa as atualizações.
