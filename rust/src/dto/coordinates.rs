#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct PositionDto {
    /// 1-based line number
    pub line: u32,
    /// 1-based column (Unicode codepoints)
    pub column: u32,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct RangeDto {
    pub start: PositionDto,
    pub end: PositionDto,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct FileRangeDto {
    pub path: String,
    pub range: RangeDto,
}
