"""Alert notification manager.

Posts alerts to the warehouse webhook in the same payload format used by
python/dexter_alert.py, ensuring identical alert records regardless of
whether the strategy runs on QuantConnect cloud or locally.
"""

import json


class AlertManager:
    """Fire webhook/email notifications and persist alert history."""

    def __init__(self, algorithm):
        self._algorithm = algorithm
        self._alert_log = []

    def fire_alert(self, model_name, symbol, direction, strength,
                   trigger_values=None, context_values=None):
        """Fire a notification and persist the alert.

        Posts to the warehouse webhook in the standard format:
            timestamp, symbol, price, alert_type, entry_exit, side,
            bar_size, strength, entry_timestamp, source
        """
        trigger_values = trigger_values or {}
        context_values = context_values or {}

        # Determine entry vs exit
        action_type = trigger_values.get("action_type", "")
        is_exit = direction == "INVALIDATED" or action_type == "EXIT"

        # Build warehouse-compatible payload
        payload = {
            "timestamp": str(self._algorithm.Time),
            "symbol": str(symbol),
            "price": trigger_values.get("close", 0),
            "alert_type": model_name.upper(),
            "entry_exit": "exit" if is_exit else "entry",
            "side": action_type.lower() if action_type and not is_exit else "",
            "bar_size": "10m",
            "strength": strength,
            "source": "lean-cloud",
        }

        # For exits, include the original entry timestamp
        entry_ts = trigger_values.get("entry_timestamp")
        if entry_ts:
            payload["entry_timestamp"] = str(entry_ts)

        # Internal log (full detail)
        alert = {
            **payload,
            "direction": direction,
            "trigger_values": trigger_values,
            "context_values": context_values,
        }
        self._alert_log.append(alert)

        # Webhook notification
        webhook_url = self._algorithm.GetParameter("webhook_url")
        webhook_api_key = self._algorithm.GetParameter("webhook_api_key")
        if webhook_url:
            try:
                headers = {"Content-Type": "application/json"}
                if webhook_api_key:
                    headers["X-API-Key"] = webhook_api_key
                self._algorithm.Notify.Web(
                    webhook_url,
                    json.dumps(payload),
                    headers,
                )
            except Exception as e:
                self._algorithm.Debug(f"[ALERT] Webhook failed: {e}")

        # Email notification
        email = self._algorithm.GetParameter("alert_email")
        if email:
            try:
                subject = (
                    f"ShoulderTaps: {model_name} "
                    f"{'EXIT' if is_exit else direction} on {symbol}"
                )
                self._algorithm.Notify.Email(
                    email, subject, json.dumps(alert, indent=2),
                )
            except Exception as e:
                self._algorithm.Debug(f"[ALERT] Email failed: {e}")

    def persist(self):
        """Save alert history to ObjectStore."""
        try:
            self._algorithm.ObjectStore.Save(
                "shoulder_taps/alert_history",
                json.dumps(self._alert_log),
            )
        except Exception as e:
            self._algorithm.Debug(f"[ALERT] Persist failed: {e}")

    def load_history(self):
        """Load alert history from ObjectStore."""
        key = "shoulder_taps/alert_history"
        if self._algorithm.ObjectStore.ContainsKey(key):
            try:
                return json.loads(self._algorithm.ObjectStore.Read(key))
            except Exception:
                pass
        return []

    def get_alert_count(self):
        """Return total alerts fired this session."""
        return len(self._alert_log)

    def get_alerts_by_model(self, model_name):
        """Return alerts for a specific model."""
        return [a for a in self._alert_log if a["model"] == model_name]
