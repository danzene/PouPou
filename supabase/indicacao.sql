-- Indique e ganhe (programa de indicação) — colunas e índices no profiles.
-- IDEMPOTENTE: pode rodar várias vezes sem erro. Já aplicado em produção; este arquivo
-- documenta o schema e permite recriar o banco do zero.
--
-- Regra: a cada 2 indicados que ASSINAREM, o padrinho ganha 1 mês grátis; o indicado
-- ganha 15 dias de bônus na 1ª assinatura. Ver api/main.py (_processar_indicacao).

ALTER TABLE profiles ADD COLUMN IF NOT EXISTS ref_code        text;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS indicado_por    text;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS ref_creditado   boolean DEFAULT false;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS ref_meses_dados integer DEFAULT 0;
-- perk do INDICADO: trial dobrado (7 dias em vez de 3) no cadastro. Guardado por usuário
-- ao iniciar o trial; _premium respeita este valor. Default 3 = trial normal.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS trial_dias      integer DEFAULT 3;

-- ref_code é único (quando preenchido) — é a "chave pública" do link de indicação.
CREATE UNIQUE INDEX IF NOT EXISTS profiles_ref_code_uidx
    ON profiles (ref_code) WHERE ref_code IS NOT NULL;

-- busca rápida por "quem foi indicado por este código" (contagem de convertidos).
CREATE INDEX IF NOT EXISTS profiles_indicado_por_idx
    ON profiles (indicado_por);
