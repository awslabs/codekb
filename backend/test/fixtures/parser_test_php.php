<?php

namespace App\Services;

const MAX_USERS = 100;
const DEFAULT_ROLE = "member";

function clean_input(string $value): string
{
    return trim($value);
}

function is_empty(string $value): bool
{
    return clean_input($value) === "";
}

interface UserRepository
{
    public function save(User $user): void;
}

trait Loggable
{
    public function log(string $message): void
    {
        echo $message;
    }
}

enum UserRole
{
    case Admin;
    case Member;
}

class StringHelper
{
    public static function format(string $value): string
    {
        return clean_input($value);
    }
}

class UserService implements UserRepository
{
    const MAX_RETRIES = 3;
    private string $name;
    public static int $count = 0;

    public function __construct(string $name)
    {
        $this->name = $name;
    }

    public function save(User $user): void
    {
        $this->notifyAdmin();
        $this->validateUser($user);
        StringHelper::format($user->name);
    }

    private function validateUser(User $user): void
    {
        is_empty($user->name);
    }

    private function notifyAdmin(): void
    {
        echo "admin notified";
    }

    public static function version(): string
    {
        return "1.0";
    }
}

class UserProcessor
{
    public function process(string $name): void
    {
        $service = new UserService($name);
        $service->save(new User());
        $clean = clean_input($name);
    }
}
