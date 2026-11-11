# Agentic Discord

클로드가 대화 중에 디스코드를 사용자처럼 읽게 하는 Claude Code 스킬(`discord`). 서버·채널·스레드·DM을 나열하고, 채널을 기간·범위로 읽어 대화록으로 보고, 디스코드 자체 검색을 쓰고, 받은 메시지를 로컬 SQLite 보관소에 쌓아 SQL로 분석한다. 읽기 전용이다: HTTP GET만 보내며 전송·반응·읽음 표시는 없다.

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

토큰은 브라우저에서 discord.com에 로그인한 뒤 개발자 도구로 얻는다. 방법은 DiscordChatExporter의 [Token and IDs](https://github.com/Tyrrrz/DiscordChatExporter/blob/master/.docs/Token-and-IDs.md) 문서에 정리되어 있다. 토큰을 바꾸려면 디스코드 비밀번호를 바꾼다.

## 데이터가 남는 곳

- 보관소: `.claude/skills/discord/data/discord.db` (모든 프로젝트가 함께 쓴다, gitignore)
- 호출별 결과 파일: 작업 디렉터리의 `.discord/results/` (스스로 gitignore한다)

둘 다 다른 사람들이 쓴 메시지(DM 포함)를 담는다. 읽을 수 있다는 것이 공유해도 된다는 뜻은 아니다.

## 주의

사용자 토큰으로 디스코드를 자동화하는 것은 [디스코드 약관 위반](https://support.discord.com/hc/en-us/articles/115002192352-Automated-user-accounts-self-bots-)이고 계정 제한으로 이어질 수 있다. 이 스킬은 요청을 하나씩 0.5초 간격으로 보내고 재시도를 디스코드가 요구하는 만큼만 하지만, 그것으로 위험이 없어지지는 않는다.

## 개발

```bash
python3 -m unittest discover -s tests
```

가짜 디스코드 서버로 돌며 네트워크를 쓰지 않는다. 설계와 실측 근거는 [`.claude/plans/discord-skill.md`](.claude/plans/discord-skill.md)에 있다.
