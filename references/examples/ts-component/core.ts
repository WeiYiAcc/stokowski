/**
 * Implementation details — bases import from index.ts, not here.
 *
 * Uses Effect for typed error handling:
 * - Success path returns GreetResult
 * - Errors tracked in type signature
 */

import { Effect } from "effect";

export interface GreetResult {
  message: string;
  name: string;
}

export class EmptyNameError {
  readonly _tag = "EmptyNameError";
}

export const greet = (
  name: string
): Effect.Effect<GreetResult, EmptyNameError> =>
  name.trim()
    ? Effect.succeed({ message: `Hello, ${name}!`, name })
    : Effect.fail(new EmptyNameError());

export const greetAll = (
  names: string[]
): Effect.Effect<GreetResult[], EmptyNameError> =>
  Effect.all(names.map(greet));
