#!/bin/bash
# run.sh — Start Larry's agents
# Usage:
#   ./run.sh          — start both agents
#   ./run.sh betting  — start only betting agent
#   ./run.sh twitter  — start only twitter agent
#   ./run.sh stop     — stop all agents

LOGDIR="/home/larry/logs"
mkdir -p $LOGDIR

case "$1" in
  stop)
    echo "Stopping Larry's agents..."
    pkill -f "betting_agent.py" && echo "✅ Betting agent stopped"
    pkill -f "twitter_agent.py" && echo "✅ Twitter agent stopped"
    ;;
  betting)
    echo "🎰 Starting betting agent only..."
    nohup python betting_agent.py >> $LOGDIR/betting.log 2>&1 &
    echo "PID: $!"
    ;;
  twitter)
    echo "🐦 Starting twitter agent only..."
    nohup python twitter_agent.py >> $LOGDIR/twitter.log 2>&1 &
    echo "PID: $!"
    ;;
  *)
    echo "🚀 Starting Larry — both agents..."
    nohup python betting_agent.py >> $LOGDIR/betting.log 2>&1 &
    echo "Betting agent PID: $!"
    sleep 2
    nohup python twitter_agent.py >> $LOGDIR/twitter.log 2>&1 &
    echo "Twitter agent PID: $!"
    echo ""
    echo "✅ Larry is LIVE. Check logs:"
    echo "  tail -f $LOGDIR/betting.log"
    echo "  tail -f $LOGDIR/twitter.log"
    ;;
esac
