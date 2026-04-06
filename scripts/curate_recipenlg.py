from dms.recipenlg_pipeline import compile_recipenlg_dataset, write_recipenlg_outputs


def main() -> None:
    rows, stats = compile_recipenlg_dataset()
    parquet_key, meta_key = write_recipenlg_outputs(rows, stats)
    print(parquet_key)
    print(meta_key)


if __name__ == "__main__":
    main()
