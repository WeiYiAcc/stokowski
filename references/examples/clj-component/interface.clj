(ns my-project.greeter.interface
  "Greeter — minimal Polylith component example.
   Interface re-exports public API only.")

(defn greet
  "Generate a greeting for the given name."
  [name]
  (str "Hello, " name "!"))

(defn greet-all
  "Generate greetings for a list of names."
  [names]
  (mapv greet names))
