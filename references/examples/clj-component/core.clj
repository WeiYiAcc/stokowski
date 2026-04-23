(ns my-project.greeter.core
  "Implementation details — NOT imported directly by bases.
   Bases only use the interface namespace.")

(defn- format-greeting [template name]
  (clojure.string/replace template "{name}" name))

;; If interface needs core, interface.clj would:
;; (:require [my-project.greeter.core :as core])
;; (defn greet [name] (core/format-greeting "Hello, {name}!" name))
