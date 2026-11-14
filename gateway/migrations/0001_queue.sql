CREATE TABLE `jobs` (
	`id` text PRIMARY KEY NOT NULL,
	`owner` text NOT NULL,
	`action` text NOT NULL,
	`digest` text NOT NULL,
	`status` text NOT NULL,
	`created` integer NOT NULL,
	`expires` integer NOT NULL,
	`lease` text,
	`lease_until` integer,
	`result_key` text
);
--> statement-breakpoint
CREATE INDEX `job_pending` ON `jobs` (`status`,`created`);--> statement-breakpoint
CREATE TABLE `state` (
	`key` text PRIMARY KEY NOT NULL,
	`value` text NOT NULL
);
