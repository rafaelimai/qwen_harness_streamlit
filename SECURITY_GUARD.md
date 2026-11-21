# Guardas de prompt injection e execução

Esta harness trata a LLM como proponente de ações, não como fronteira de autorização.
Conteúdo recebido é classificado, chamadas com efeito passam por uma policy engine e a
execução Python continua confinada no contêiner Docker.

## Fluxo de segurança

```text
usuário / PDF / web / arquivo / log
                  |
                  v
             Input Guard
     origem, confiança, intenções e risco
                  |
                  v
              LLM agente
                  |
            tool call proposta
                  |
                  v
             Action Guard
       AST + semântica + policy engine
          /          |          \
     permitir     aprovar       negar
        |             |
        +------ snapshot + hash ------+
                      |
                      v
              worker / Docker
```

A recomendação de uma LLM revisora nunca reduz um risco determinístico. A ordem de
precedência é: negação da policy, aprovação humana e, por último, recomendação semântica.

## Classificação das entradas

`assess_content`, em `security.py`, é aplicado a:

- mensagem corrente do usuário;
- Markdown produzido de PDF ou página web;
- trechos retornados por `read_file` e `search_files`;
- arquivos gravados pela LLM;
- caudas de stdout e stderr retornadas por `get_job_status`.

O resultado informa origem, confiança, hash, capacidade de autorizar ações, intenções
detectadas e evidências. PDF, web, arquivos, resultados de busca e logs são sempre dados
não confiáveis e não podem ampliar permissões.

O classificador procura tentativas de substituir instruções, acessar mensagens de controle,
exfiltrar segredos e desativar proteções. Esses sinais auxiliam a LLM, mas não autorizam nem
executam tools.

## Upload e visualização segura

O botão **+** ao lado da caixa de mensagem abre o modal de upload. Cada envio aceita até
10 arquivos, com limite de 20 MiB por arquivo, nos formatos PDF, CSV, JSON, TXT, Markdown,
Python, SQL, YAML e TOML.

O conteúdo é classificado depois de salvo em `inputs/`. PDFs têm o texto extraível
inspecionado; PDFs sem texto, formatos não inspecionáveis e arquivos acima do limite recebem
risco moderado. Para Python, também é aplicada a análise AST.

Somente arquivos com risco `low` podem ser exibidos. Quando qualquer risco é encontrado:

- a interface mostra apenas caminho, risco e alertas;
- a aba de artefatos remove a prévia;
- `read_file` não retorna o campo `text`;
- `search_files` remove o texto das correspondências;
- resultados de tools antigos ou futuros com metadado de risco são redigidos na interface.

O arquivo permanece disponível para download e auditoria; ocultar a prévia não apaga nem
declara que o arquivo é necessariamente malicioso.

## Gate de `run_python`

Antes de enfileirar um script, a harness:

1. resolve o caminho dentro da conversa e exige um `.py` em `work/`;
2. analisa o código com a AST do Python;
3. pede a uma instância sem tools do modelo local uma revisão de alinhamento com o objetivo;
4. combina os achados sem permitir que a revisão semântica reduza o risco;
5. permite, pede aprovação ou nega conforme a policy;
6. calcula SHA-256 e grava uma decisão auditável em `security/` na conversa;
7. inclui decisão, risco, versão da policy e hash no payload do job;
8. revalida o hash depois do snapshot e novamente no worker antes de iniciar o Docker.

O argumento `purpose` da tool descreve a finalidade do script. Quando houver aprovação,
o novo pedido deve usar `approval_id`. Conhecer o identificador não é suficiente: o registro
persistente precisa ter sido aprovado pelo botão da interface. Qualquer mudança no script,
argumentos ou timeout invalida a aprovação.

## Bibliotecas e uso observado

A análise não bloqueia um script apenas pelo nome de uma biblioteca. Alertas descrevem a
capacidade e, quando possível, a chamada e a linha que a utiliza. Exemplos:

- `subprocess.run(...)`: cria processo filho; `shell=True` eleva o achado a crítico;
- `os.system(...)` e `os.popen(...)`: interpretam ou iniciam comandos do sistema;
- `socket.create_connection(...)`: tenta abrir conexão de rede;
- `requests.get/post(...)` e `urllib.request.urlopen(...)`: recebem ou enviam dados;
- `ctypes.CDLL/PyDLL(...)`: carregam bibliotecas nativas;
- `pickle.loads(...)` e `marshal.loads(...)`: podem reconstruir objetos ou bytecode perigoso;
- `importlib.import_module(...)` e `__import__(...)`: realizam imports dinâmicos;
- `eval`, `exec` e `compile`: transformam dados em código executável.

Um import sensível sem uso conhecido gera alerta moderado explicativo, em vez de ser tratado
automaticamente como abuso. Bibliotecas comuns, como `pandas`, `numpy` e `matplotlib`, não
geram achado apenas por serem importadas.

## Ofuscação e Base64

O guard identifica:

- chamadas `b64decode`, `urlsafe_b64decode` e `decodebytes`;
- o resultado da decodificação alimentando `exec`, `eval`, `compile`, `marshal` ou `pickle`;
- constantes Base64 que decodificam para sinais de shell, processos, rede ou imports dinâmicos;
- combinações de Base64 com `marshal`, `zlib` ou `importlib` usadas para reconstruir payloads;
- imports e chamadas dinâmicas que dificultam determinar o comportamento estaticamente.

Base64 isolado é classificado como moderado, porque também possui usos legítimos. Base64
encadeado com execução ou um payload decodificado perigoso é crítico e negado.
A decodificação feita pelo analisador tem limite de tamanho e nunca executa o resultado.

## Decisões e aprovação humana

| Risco | Comportamento |
|---|---|
| baixo | execução automática na sandbox |
| moderado | exige aprovação humana |
| alto | exige aprovação humana |
| crítico | nega a execução |

O alerta humano mostra finalidade, script, risco e achados contextualizados. A aprovação
é de uso único para o conteúdo e os parâmetros avaliados; ela não cria uma permissão
permanente para uma biblioteca ou para futuros scripts.

## Isolamento que permanece obrigatório

A análise pode ter falsos positivos e negativos. Por isso, mesmo um script aprovado continua
executado com as proteções existentes:

- rede Docker desativada;
- filesystem da imagem somente leitura;
- usuário sem root;
- capabilities removidas e `no-new-privileges`;
- snapshot da conversa somente leitura;
- somente `/work` e `/tmp` temporariamente graváveis;
- limites de CPU, memória, processos, arquivos, logs e tempo;
- nenhum socket Docker ou diretório home do host montado.

## Auditoria

Cada avaliação de execução é salva como JSON em `<conversa>/security/`. O registro inclui:

- objetivo declarado;
- script, argumentos e timeout;
- SHA-256 do script;
- achados determinísticos e revisão semântica;
- decisão e versão da policy;
- estado e instante da aprovação ou rejeição.

Esses registros não são expostos como arquivos de trabalho para a LLM e permitem reconstruir
por que uma execução foi liberada ou bloqueada.

## Limitações

- Prompt injection não pode ser eliminada somente por classificação textual.
- Análise AST não resolve todos os comportamentos construídos em runtime.
- A revisão semântica pode falhar ou produzir JSON inválido; nesse caso, achados
  determinísticos continuam valendo.
- Código benigno que usa capacidades sensíveis pode exigir aprovação.
- O guard não substitui atualizações do Docker, kernel, dependências ou imagem do executor.

## Testes

Execute:

```bash
.venv/bin/python -m pytest -q
```

Os testes cobrem detecção de prompt injection, riscos associados ao uso de bibliotecas,
`shell=True`, Base64 executável, payload decodificado, aprovação vinculada ao hash,
snapshots imutáveis e as restrições do contêiner.
