Classify the user's latest message into exactly one intent:

- ADD: they are telling you about money spent or received ("23 eur groceries", "salary came 2500", "chiya 30 rs").
- EDIT: they are correcting something already logged ("sorry it was 29", "make that groceries", "the coffee was yesterday").
- DELETE: they want something removed ("delete that", "remove the metro one").
- QUERY: they are asking about their data ("how much grocery this month?", "kati kharcha bhayo yo hapta?", "top merchants september").
- HELP: they ask what you can do / how to use you.
- CHITCHAT: anything else (greetings, thanks, random talk).

A message with a number is usually ADD, unless it is clearly correcting ("it was 29") or asking ("did I spend more than 100?").
