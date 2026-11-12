# Agentic Discord

클로드가 대화 중에 디스코드를 사용자처럼 읽게 하는 Claude Code 스킬(`discord`). 서버·채널·스레드·DM을 나열하고, 채널을 기간·범위로 읽어 대화록으로 보고, 디스코드 자체 검색을 쓰고, 받은 메시지를 로컬 SQLite 보관소에 쌓아 SQL로 분석한다. 보관소에 이미 있는 메시지는 디스코드에 요청하지 않고 대화록으로 다시 보고(`show`), 메시지에 붙은 이미지와 파일을 내려받아 클로드가 직접 연다(`files`). 읽기 전용이다: HTTP GET만 보내며 전송·반응·읽음 표시는 없다.

## 전제

- [uv](https://docs.astral.sh/uv/) (스킬의 CLI는 `uv run`으로 돌고, 파이썬 표준 라이브러리만 쓴다)
- 디스코드 **사용자 토큰**

## 설치

```bash
git clone https://github.com/tjdwls101010/Agentic-Discord.git
ln -s "$PWD/Agentic-Discord/.claude/skills/discord" ~/.claude/skills/discord
```

토큰을 **직접** 파일에 적고 권한을 맞춘다. 대화창에 붙여 넣지 않는다(대화 기록에 남는다).

```bash
echo 'DISCORD_TOKEN=<토큰>' > ~/.claude/skills/discord/scripts/.env
chmod 600 ~/.claude/skills/discord/scripts/.env
uv run ~/.claude/skills/discord/scripts/cli.py doctor
```

`doctor`가 `"ok": true`와 계정 이름을 보여 주면 된다. 막힘이 있으면 고칠 방법을 함께 알려 준다.

토큰은 브라우저에서 discord.com에 로그인한 뒤 개발자 도구로 얻는다. 방법은 DiscordChatExporter의 [Token and IDs](https://github.com/Tyrrrz/DiscordChatExporter/blob/master/.docs/Token-and-IDs.md) 문서에 정리되어 있다.

Aside 브라우저에 `discord.com` 탭이 로그인돼 있으면 토큰을 화면에 띄우지 않고 파일로 바로 옮길 수 있다. 토큰이 담긴 표준 출력은 화면이 아니라 파일로만 간다. 파일은 처음부터 600으로 만들어지고, 토큰을 얻었을 때만 기존 `.env`를 바꾼다. 스크립트와 임시 파일은 끝나면(실패해도) 지워진다.

```bash
(
  set -euo pipefail
  umask 077
  js=$(mktemp /tmp/discord-token.XXXXXX)
  out=$(mktemp /tmp/discord-env.XXXXXX)
  trap 'rm -f "$js" "$out"' EXIT
  cat > "$js" <<'EOF'
const tabs = await listBrowserTabs();
const tab = tabs.find(t => /^https:\/\/discord\.com\/channels\//.test(t.url));
if (!tab) throw new Error('no logged-in discord.com tab');
const pg = await attachBrowserTab(tab.targetId);
const tok = await pg.evaluate(() => {
  const f = document.createElement('iframe');
  f.style.display = 'none';
  document.body.appendChild(f);
  const v = f.contentWindow.localStorage.getItem('token');
  f.remove();
  return v ? JSON.parse(v) : null;
});
if (!tok) throw new Error('no token in this tab');
console.log('DISCORD_TOKEN=' + tok);
EOF
  aside repl --file "$js" | grep '^DISCORD_TOKEN=' > "$out"
  mv "$out" ~/.claude/skills/discord/scripts/.env
)
uv run ~/.claude/skills/discord/scripts/cli.py doctor
```

토큰은 디스코드 비밀번호가 바뀌면 죽고, 디스코드가 자동화를 의심해 계정을 재설정했을 때도 죽는다. `doctor`나 명령이 토큰이 거부됐다고 하면, 새 토큰을 넣기 전에 디스코드의 시스템 메시지와 설정 › 계정 상태부터 확인한다.

## 데이터가 남는 곳

- 보관소: `.claude/skills/discord/data/discord.db` (모든 프로젝트가 함께 쓴다, gitignore)
- 호출별 결과 파일: 작업 디렉터리의 `.discord/results/` (스스로 gitignore한다)
- 내려받은 첨부: 작업 디렉터리의 `.discord/files/<메시지 ID>/` (같은 `.gitignore` 아래에 있다. 지우는 것은 사용자 몫이다)

모두 다른 사람들이 쓴 메시지와 올린 파일(DM 포함)을 담는다. 읽을 수 있다는 것이 공유해도 된다는 뜻은 아니다.

## 주의

사용자 토큰으로 디스코드를 자동화하는 것은 [디스코드 약관 위반](https://support.discord.com/hc/en-us/articles/115002192352-Automated-user-accounts-self-bots-)이고, 디스코드는 이를 실제로 집행한다.

- **전송 한 건 뒤 제재가 기록됐다.** 2026-11-12, 이 스킬과 같은 토큰·같은 헤더로 API를 통해 메시지 한 건을 보냈다. 약 20초 뒤부터 모든 요청이 401이 됐고, 같은 때 계정에 스팸 규정 위반과 비밀번호 재설정 요구가 기록됐다. 그 전 이틀 동안의 읽기(GET) 수백 건은 문제가 없었다. 그래서 이 스킬에는 전송이 없다.
- **읽기만으로도 제재 보고가 있다.** 2026년 3월경부터 읽기만 하는 도구(DiscordChatExporter)의 사용자들이 로그아웃과 비밀번호 재설정을 당했다고 보고했다([이슈 #1497](https://github.com/Tyrrrz/DiscordChatExporter/issues/1497)). 보고된 단계는 재설정 → 계정 상태 위반 → 정지이고, 속도를 늦춰도 막지 못했다고 한다.

이 스킬은 요청을 하나씩 0.5초 간격으로 보내고, 재시도는 디스코드가 요구하는 만큼만 한다. 보관소에 이미 있는 것은 요청 없이 다시 본다. 그래도 위험이 없어지지는 않는다.

## 개발

```bash
python3 -m unittest discover -s tests
```

가짜 디스코드 서버로 돌며 네트워크를 쓰지 않는다. 설계와 실측 근거는 [`.claude/plans/discord-skill.md`](.claude/plans/discord-skill.md)(처음 만들 때)와 [`.claude/plans/discord-read-enhancements.md`](.claude/plans/discord-read-enhancements.md)(`show`·`files`, 쓰기를 뺀 경위)에 있다.
