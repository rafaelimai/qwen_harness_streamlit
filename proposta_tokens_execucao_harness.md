# Harness — tokens de execução e recuperação de tarefas

## 1. Proposta resumida

**Problema.** Uma tarefa pode ser retomada por outro worker após a expiração de seu prazo (*lease*), enquanto o worker anterior ainda está vivo. Isso pode causar execução duplicada e sobrescrita de resultados.

**Minha alternativa.**

1. **Token por tentativa de execução:** ao reivindicar uma tarefa, o worker recebe um identificador exclusivo da *tentativa* (não apenas um identificador fixo do processo). O SQLite registra qual token está ativo para aquela tarefa.
2. **Invalidação:** se outro worker assumir legitimamente a tarefa, o token anterior passa a ser considerado `stale` (obsoleto). A troca de titularidade deve ocorrer de maneira atômica no banco.
3. **Cancelamento:** o worker consulta periodicamente a validade de seu token. Se estiver `stale`, interrompe a execução e solicita o encerramento do processo/container associado.
4. **Proteção do resultado:** qualquer gravação de resultado ou mudança para `completed` só é aceita se o token enviado ainda for o token ativo. A verificação e a gravação devem fazer parte da **mesma operação/transação**.
5. **Detecção de abandono:** um prazo de validade (*lease*) não renovável permite decidir quando outra tentativa pode assumir a tarefa. A expiração, por si só, não invalida o token atual: ele só se torna `stale` quando outra tentativa toma posse e substitui o token ativo atomicamente.

**Limite importante:** o cancelamento cooperativo não é instantâneo. Mesmo um worker obsoleto pode continuar executando por algum tempo; operações externas já realizadas (HTTP, arquivos compartilhados etc.) não são revertidas automaticamente. A comparação do token protege o resultado persistido **na base controlada pela harness**, não elimina todo efeito colateral.

**Objetivo de implementação:** solução pequena, com SQLite, sem Redis ou novos serviços, adequada a um MVP local.

---

## 2. Experimento para testar a harness

### Enunciado para fornecer ao agente

> Implemente um protótipo isolado, em Python 3 e SQLite (somente biblioteca padrão), para demonstrar o esquema de tokens por tentativa descrito neste documento. **Não altere os arquivos da harness existente.** Trabalhe em uma pasta de experimento e não use rede ou serviços externos.
>
> 
> O protótipo precisa permitir (a) reivindicar atomicamente uma tarefa pendente, (b) permitir nova reivindicação quando o lease não renovável expirar, (c) detectar um token substituído e (d) impedir atomicamente que esse token antigo grave o resultado final. A mera expiração não torna o token obsoleto; a obsolescência ocorre somente após uma nova tomada de posse.
>
> Crie um programa de demonstração e testes automatizados. Execute os testes se o ambiente permitir. Se não puder gravar arquivos ou executar comandos, devolva o conteúdo completo dos arquivos em blocos de código Markdown e **não diga que testou**.
>
> Antes de implementar, apresente um plano curto. Ao terminar, explique o comportamento observado, os comandos de execução, as limitações e as decisões arquiteturais. Não introduza Redis, Celery, RabbitMQ, Docker nem bibliotecas externas.

### Cenário reproduzível: tarefa #42

Use dois workers simulados (`A` e `B`) com **conexões SQLite distintas**. Controle o relógio por um valor injetado nos métodos ou por timestamps de teste, para evitar `sleep()` e testes instáveis.

| Passo | Ação | Resultado esperado |
|---|---|---|
| 1 | Inserir a tarefa `#42` com estado `pending`. | Nenhum token ativo. |
| 2 | Worker A reivindica `#42` com lease de 30 s. | A recebe token `TA`; tarefa vira `running`. |
| 3 | O lease de A expira, sem que outro worker assuma a tarefa. | `TA` continua ativo e A ainda pode concluir; a expiração apenas habilita uma tomada de posse. |
| 4 | Worker B reivindica `#42` após a expiração. | B recebe token `TB`, diferente de `TA`; `TB` é o token ativo. |
| 5 | Worker A tenta salvar `resultado_A`. | **Rejeitado:** zero linhas atualizadas; nenhum resultado é gravado. |
| 6 | Worker A consulta seu token. | Identifica `TA` como `stale` e inicia cancelamento da execução. |
| 7 | Worker B salva `resultado_B`. | **Aceito:** tarefa fica `completed`, com `resultado_B`. |
| 8 | Worker A tenta salvar novamente. | **Rejeitado:** o resultado final continua `resultado_B`. |

### Segundo teste obrigatório: disputa simultânea

Crie uma nova tarefa `#43` no estado `pending`. Faça dois workers disputarem sua reivindicação, com conexões SQLite independentes e início sincronizado por barreira ou mecanismo equivalente. **Exatamente um** deve conseguir a tarefa. O perdedor deve tratar bloqueios transitórios com espera ou repetição limitada e, ao observar o estado atualizado, retornar “nenhuma tarefa disponível”. Um `database is locked` persistente deve falhar explicitamente no teste, e não ser aceito como resultado normal. Repita o cenário com 20 tarefas independentes, usando novas conexões em cada rodada.

O teste deve usar um arquivo SQLite compartilhado, e não bancos `:memory:` independentes. O relógio pode ser injetado, mas a disputa deve ocorrer por transações reais em conexões distintas. Não use uma trava Python compartilhada para serializar artificialmente a reivindicação.

### Modelo mínimo sugerido

Cada tarefa deve conter, no mínimo:

- `id`;
- `status` (`pending`, `running` ou `completed`);
- `active_token`;
- `lease_expires_at`, usado somente para autorizar uma nova tomada de posse;
- `result`;
- `attempt_number`;
- `updated_at`.

O token ativo é a autoridade para concluir a tarefa. `lease_expires_at` não participa da validação da conclusão: depois de expirar, o token anterior permanece válido até ser substituído atomicamente por outra tentativa.

### Testes obrigatórios

Os testes devem se limitar ao risco de duas execuções da mesma tarefa. Uma tarefa apenas pendurada ou com lease expirado, sem uma tentativa concorrente que tome posse, não ativa o fencing.

1. **Reivindicação inicial concorrente:** dois workers disputam uma tarefa `pending`; exatamente um recebe o token ativo.
2. **Sem tomada de posse antes do prazo:** outro worker não consegue assumir uma tarefa `running` cujo lease ainda não expirou.
3. **Expiração sem concorrente:** a expiração isolada não invalida o token; o titular ainda consegue concluir enquanto não houver substituição.
4. **Tomada de posse após expiração:** um segundo worker assume atomicamente, recebe um token diferente e torna o anterior `stale`.
5. **Fencing do resultado:** após a tomada de posse, o token antigo não consegue concluir, gravar falha, cancelar nem sobrescrever o resultado.
6. **Conclusão do novo titular:** o token novo conclui a tarefa e grava o resultado; tentativas posteriores do token antigo não alteram o estado final.
7. **Disputa pela retomada:** dois workers tentam assumir simultaneamente a mesma tarefa expirada; exatamente um se torna o novo titular.
8. **Estado terminal:** uma tarefa `completed` não pode ser reivindicada ou retomada, ainda que o prazo anteriormente registrado tenha expirado.

### Critérios objetivos de aprovação

- [ ] **Atomicidade:** dois workers não reivindicam simultaneamente a mesma tentativa pendente.
- [ ] **Retomada:** após expirar o lease, outro worker pode assumir, obtendo novo token; sem essa tomada de posse, o token anterior continua válido.
- [ ] **Fencing:** o token antigo não consegue concluir nem sobrescrever a tarefa.
- [ ] **Cancelamento:** o worker antigo detecta o token obsoleto e registra/aciona a intenção de parar (não é necessário matar um container real neste MVP).
- [ ] **Testabilidade:** há testes automatizados reproduzíveis, sem dependência de relógio real ou serviço externo.
- [ ] **Transparência:** o agente diferencia código realmente executado de código apenas proposto.

**Falha crítica:** uma solução que primeiro verifica o token e depois grava o resultado, sem garantir atomicidade entre as duas ações, **não passa**, pois permite outra condição de corrida. Prefira uma única instrução condicional, por exemplo `UPDATE ... WHERE id = ? AND status = 'running' AND active_token = ?`, considerando a operação bem-sucedida somente quando exatamente uma linha for alterada.

### Entregáveis esperados do agente

1. Código Python do protótipo, usando SQLite.
2. Testes automatizados para os cenários obrigatórios.
3. Comandos exatos para executar os testes e saída real observada, se executados.
4. Um resumo curto de como evita sobrescrita e do que ainda não evita (efeitos colaterais externos, por exemplo).

### Como avaliar a capacidade da harness (0–10)

Avalie primeiro a correção do protocolo e, em seguida, a capacidade operacional da harness.

#### 1. Correção do protocolo (0–8)

| Pontos | Evidência |
|---|---|
| 2 | Entendeu a diferença entre tarefa, worker e tentativa/token, inclusive que a expiração isolada não invalida o token. |
| 2 | Implementou a reivindicação inicial e a tomada de posse de forma atômica no SQLite. |
| 2 | Após a tomada de posse, impediu o token antigo de concluir ou alterar o estado da tarefa. |
| 2 | Implementou e demonstrou os cenários concorrentes obrigatórios sem serialização artificial em Python. |

#### 2. Capacidade operacional da harness (0–2)

| Pontos | Evidência |
|---|---|
| 1 | Criou corretamente os artefatos solicitados e informou comandos reproduzíveis. |
| 1 | Executou os testes quando o ambiente permitiu e distinguiu resultados reais de código apenas proposto. |

**Observação:** o exercício testa tanto o raciocínio do modelo quanto a capacidade da harness de criar artefatos, chamar ferramentas, executar código e reportar falhas. Se a harness ainda não permite criar `.py`, aceite como saída intermediária os arquivos descritos em blocos de código e execute-os manualmente em ambiente isolado.
